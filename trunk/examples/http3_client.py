import argparse
import asyncio
import dataclasses
import logging
import os
import pickle
import ssl
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import BinaryIO, Callable, Deque, Dict, List, Optional, Union, cast
from urllib.parse import urlparse

import wsproto
import wsproto.events

import aioquic
from aioquic.asyncio.client import connect
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.h0.connection import H0_ALPN, H0Connection
from aioquic.h3.connection import H3_ALPN, ErrorCode, H3Connection
from aioquic.h3.events import (
    DataReceived,
    H3Event,
    HeadersReceived,
    PushPromiseReceived,
)
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import QuicEvent
from aioquic.quic.logger import QuicFileLogger, QuicSingleFileLogger
from aioquic.tls import CipherSuite, SessionTicket

try:
    import uvloop
except ImportError:
    uvloop = None

logger = logging.getLogger("client")

HttpConnection = Union[H0Connection, H3Connection]

USER_AGENT = "aioquic/" + aioquic.__version__


class URL:
    def __init__(self, url: str) -> None:
        parsed = urlparse(url)

        self.authority = parsed.netloc
        self.full_path = parsed.path or "/"
        if parsed.query:
            self.full_path += "?" + parsed.query
        self.scheme = parsed.scheme


class HttpRequest:
    def __init__(
        self,
        method: str,
        url: URL,
        content: bytes = b"",
        headers: Optional[Dict] = None,
    ) -> None:
        if headers is None:
            headers = {}

        self.content = content
        self.headers = headers
        self.method = method
        self.url = url


class WebSocket:
    def __init__(
        self, http: HttpConnection, stream_id: int, transmit: Callable[[], None]
    ) -> None:
        self.http = http
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.stream_id = stream_id
        self.subprotocol: Optional[str] = None
        self.transmit = transmit
        self.websocket = wsproto.Connection(wsproto.ConnectionType.CLIENT)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """
        Perform the closing handshake.
        """
        data = self.websocket.send(
            wsproto.events.CloseConnection(code=code, reason=reason)
        )
        self.http.send_data(stream_id=self.stream_id, data=data, end_stream=True)
        self.transmit()

    async def recv(self) -> str:
        """
        Receive the next message.
        """
        return await self.queue.get()

    async def send(self, message: str) -> None:
        """
        Send a message.
        """
        assert isinstance(message, str)

        data = self.websocket.send(wsproto.events.TextMessage(data=message))
        self.http.send_data(stream_id=self.stream_id, data=data, end_stream=False)
        self.transmit()

    def http_event_received(self, event: H3Event) -> None:
        if isinstance(event, HeadersReceived):
            for header, value in event.headers:
                if header == b"sec-websocket-protocol":
                    self.subprotocol = value.decode()
        elif isinstance(event, DataReceived):
            self.websocket.receive_data(event.data)

        for ws_event in self.websocket.events():
            self.websocket_event_received(ws_event)

    def websocket_event_received(self, event: wsproto.events.Event) -> None:
        if isinstance(event, wsproto.events.TextMessage):
            self.queue.put_nowait(event.data)


class HttpClient(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.pushes: Dict[int, Deque[H3Event]] = {}
        self._http: Optional[HttpConnection] = None
        self._request_events: Dict[int, Deque[H3Event]] = {}
        self._request_waiter: Dict[int, asyncio.Future[Deque[H3Event]]] = {}
        self._websockets: Dict[int, WebSocket] = {}

        if self._quic.configuration.alpn_protocols[0].startswith("hq-"):
            self._http = H0Connection(self._quic)
        else:
            self._http = H3Connection(self._quic)

    async def get(self, experiment, headers: Optional[Dict] = None) -> Deque[H3Event]:
        """
        Perform a GET request.
        """
        return await self._request(
            HttpRequest(method="GET", url=URL(experiment["url"]), headers=headers),
            experiment
        )

    async def post(
        self, url: str, data: bytes, headers: Optional[Dict] = None
    ) -> Deque[H3Event]:
        """
        Perform a POST request.
        """
        return await self._request(
            HttpRequest(method="POST", url=URL(url), content=data, headers=headers)
        )

    async def websocket(
        self, url: str, subprotocols: Optional[List[str]] = None
    ) -> WebSocket:
        """
        Open a WebSocket.
        """
        request = HttpRequest(method="CONNECT", url=URL(url))
        stream_id = self._quic.get_next_available_stream_id()
        websocket = WebSocket(
            http=self._http, stream_id=stream_id, transmit=self.transmit
        )

        self._websockets[stream_id] = websocket

        headers = [
            (b":method", b"CONNECT"),
            (b":scheme", b"https"),
            (b":authority", request.url.authority.encode()),
            (b":path", request.url.full_path.encode()),
            (b":protocol", b"websocket"),
            (b"user-agent", USER_AGENT.encode()),
            (b"sec-websocket-version", b"13"),
        ]
        if subprotocols:
            headers.append(
                (b"sec-websocket-protocol", ", ".join(subprotocols).encode())
            )
        self._http.send_headers(stream_id=stream_id, headers=headers)

        self.transmit()

        return websocket

    def http_event_received(self, event: H3Event) -> None:
        if isinstance(event, (HeadersReceived, DataReceived)):
            stream_id = event.stream_id
            if stream_id in self._request_events:
                # http
                self._request_events[event.stream_id].append(event)
                if event.stream_ended:
                    request_waiter = self._request_waiter.pop(stream_id)
                    request_waiter.set_result(self._request_events.pop(stream_id))

            elif stream_id in self._websockets:
                # websocket
                websocket = self._websockets[stream_id]
                websocket.http_event_received(event)

            elif event.push_id in self.pushes:
                # push
                self.pushes[event.push_id].append(event)

        elif isinstance(event, PushPromiseReceived):
            self.pushes[event.push_id] = deque()
            self.pushes[event.push_id].append(event)

    def quic_event_received(self, event: QuicEvent) -> None:
        #  pass event to the HTTP layer
        if self._http is not None:
            for http_event in self._http.handle_event(event):
                self.http_event_received(http_event)

    async def _request(self, request: HttpRequest, experiment = None) -> Deque[H3Event]:
        stream_id = self._quic.get_next_available_stream_id()
        
        # It's important to call the QUIC and HTTP3 layer 
        # _get_or_create_stream() methods such that the sans-IO layer
        # reserves the stream for us.
        # Yielding within this coroutine otherwise results in stream ID reuse
        self._http._get_or_create_stream(stream_id) #
        self._quic._get_or_create_stream_for_send(stream_id)

        # The following three lines were shifted from the original
        # bottom of this method; required such that incoming data has a place to go
        # Note JH: I am not 100% sure if this will happen, but not bad practice either probably
        waiter = self._loop.create_future()
        self._request_events[stream_id] = deque()
        self._request_waiter[stream_id] = waiter

        # Priority data
        pre_priority_frame = experiment.get("pre_request_priority_frame")
        post_priority_frame = experiment.get("post_request_priority_frame")
        priority_header = experiment.get("request_priority_header")
        reprioritization_priority_frame = experiment.get("reprioritization_priority_frame")
        
        if pre_priority_frame:
            logger.info(f"Pre-priority configuration found for request [{request.url.full_path}] | Setting priority to [{pre_priority_frame.get_priority_field()}] then delaying GET request with {pre_priority_frame.delay} seconds.")
            self._http.send_priority_frame(stream_id, pre_priority_frame.urgency, pre_priority_frame.incremental)
            self.transmit()
            if pre_priority_frame.delay and pre_priority_frame.delay > 0:
                await asyncio.sleep(pre_priority_frame.delay)

        headers = [
            (b":method", request.method.encode()),
            (b":scheme", request.url.scheme.encode()),
            (b":authority", request.url.authority.encode()),
            (b":path", request.url.full_path.encode()),
            (b"user-agent", USER_AGENT.encode()),
        ] + [(k.encode(), v.encode()) for (k, v) in request.headers.items()]
        if priority_header:
            headers.append((b"priority", priority_header.get_priority_field().encode()))
        
        self._http.send_headers(
            stream_id=stream_id,
            headers=headers,
            end_stream=not request.content,
        )
        if request.content:
            self._http.send_data(
                stream_id=stream_id, data=request.content, end_stream=True
            )
        self.transmit()

        if post_priority_frame:
            logger.info(f"Post-priority configuration found for request [{request.url.full_path}] | Setting priority [{post_priority_frame.get_priority_field()}] after a delay of {post_priority_frame.delay} seconds.")
            if post_priority_frame.delay and post_priority_frame.delay > 0:
                await asyncio.sleep(post_priority_frame.delay)
            self._http.send_priority_frame(stream_id, post_priority_frame.urgency, post_priority_frame.incremental)
            self.transmit()

        if reprioritization_priority_frame:
            logger.info(f"Reprioritization configuration found for request [{request.url.full_path}] | Setting priority [{reprioritization_priority_frame.get_priority_field()}] after a delay of {reprioritization_priority_frame.delay} seconds.")
            if reprioritization_priority_frame.delay and reprioritization_priority_frame.delay > 0:
                await asyncio.sleep(reprioritization_priority_frame.delay)
            self._http.send_priority_frame(stream_id, reprioritization_priority_frame.urgency, reprioritization_priority_frame.incremental)
            self.transmit()

        return await asyncio.shield(waiter)


async def perform_http_request(
    client: HttpClient,
    experiment,
    # url: str,
    data: Optional[str],
    include: bool,
    # delay_s: float,
    output_dir: Optional[str],
    # headers: Optional[Dict] = None
) -> None:

    url = experiment["url"]
    delay_s = experiment["delay"] if experiment["delay"] is not None and experiment["delay"] >= 0 else 0

    if delay_s > 0:
        await asyncio.sleep(delay_s)
        logger.info("Successfully Delayed parallel request by %.1f seconds", delay_s)

    # perform request
    start = time.time()
    if data is not None:
        data_bytes = data.encode()
        http_events = await client.post(
            url,
            data=data_bytes,
            headers={
                "content-length": str(len(data_bytes)),
                "content-type": "application/x-www-form-urlencoded",
            },
        )
        method = "POST"
    else:
        http_events = await client.get(experiment)
        method = "GET"
    elapsed = time.time() - start

    # print speed
    octets = 0
    for http_event in http_events:
        if isinstance(http_event, DataReceived):
            octets += len(http_event.data)
    logger.info(
        "Response received for %s %s : %d bytes in %.1f s (%.3f Mbps)"
        % (method, urlparse(url).path, octets, elapsed, octets * 8 / elapsed / 1000000)
    )

    # output response
    if output_dir is not None:
        output_path = os.path.join(
            output_dir, os.path.basename(urlparse(url).path) or "index.html"
        )
        with open(output_path, "wb") as output_file:
            write_response(
                http_events=http_events, include=include, output_file=output_file
            )


def process_http_pushes(
    client: HttpClient,
    include: bool,
    output_dir: Optional[str],
) -> None:
    for _, http_events in client.pushes.items():
        method = ""
        octets = 0
        path = ""
        for http_event in http_events:
            if isinstance(http_event, DataReceived):
                octets += len(http_event.data)
            elif isinstance(http_event, PushPromiseReceived):
                for header, value in http_event.headers:
                    if header == b":method":
                        method = value.decode()
                    elif header == b":path":
                        path = value.decode()
        logger.info("Push received for %s %s : %s bytes", method, path, octets)

        # output response
        if output_dir is not None:
            output_path = os.path.join(
                output_dir, os.path.basename(path) or "index.html"
            )
            with open(output_path, "wb") as output_file:
                write_response(
                    http_events=http_events, include=include, output_file=output_file
                )


def write_response(
    http_events: Deque[H3Event], output_file: BinaryIO, include: bool
) -> None:
    for http_event in http_events:
        if isinstance(http_event, HeadersReceived) and include:
            headers = b""
            for k, v in http_event.headers:
                headers += k + b": " + v + b"\r\n"
            if headers:
                output_file.write(headers + b"\r\n")
        elif isinstance(http_event, DataReceived):
            output_file.write(http_event.data)


def save_session_ticket(ticket: SessionTicket) -> None:
    """
    Callback which is invoked by the TLS engine when a new session ticket
    is received.
    """
    logger.info("New session ticket received")
    if args.session_ticket:
        with open(args.session_ticket, "wb") as fp:
            pickle.dump(ticket, fp)

@dataclass
class RequestPriorityConfiguration:
    '''Request priority configuration dataclass
    If no configuration is supplied, a default object is created that represents the default priority
    See:
    - https://www.rfc-editor.org/rfc/rfc9218.html#name-urgency
    - https://www.rfc-editor.org/rfc/rfc9218.html#name-incremental 
    '''
    urgency: int = 3
    incremental: bool = False
    delay: float  = 0  # Will only be applied on pre- and post-priority update frames, NOT on headers; 0 by default

    def get_priority_field(self) -> str:
        # Helper function to avoid making accidental typos in our experiments
        return f"u={self.urgency}" + (", i" if self.incremental else "")

@dataclass
class ExperimentRequestContext:
    '''Dataclass for experiment context
    The length of all lists MUST match!
    Call selfcheck() to make sure we did not programmatically misconfigure the object.
    
    The configurations belonging to the same index will all get used during an experiment.
    Example: if request_priority_headers and pre_request_priority_frames have a non-None value at index 1, then both will be applied

    We manipulate the field names too in get_experiment_setup(), make sure every fieldname has a plural name
    '''
    
    urls: List[str]
    delays: List[float]  # 0 equals no delay, ensure the values are positive
    request_priority_headers: List[RequestPriorityConfiguration | None]  # Insert None if no header config is required for url i
    pre_request_priority_frames: List[RequestPriorityConfiguration | None] # Insert None if no pre-request priority frame config is required for url i
    post_request_priority_frames: List[RequestPriorityConfiguration | None] # Insert None if no post-request priority frame config is required for url i

    # Afterthought param with quick and dirty fix; set to none if not to be used
    # Otherwise works like the params above
    # TODO: Redo this if ever used again
    reprioritization_priority_frames: List[RequestPriorityConfiguration | None] | None
    def _reprio_fix(self) -> None:
        # CAN ONLY BE called once
        if not self.reprioritization_priority_frames:
            # selfcheck will fail either way if the lengths mismatch, we can safely take any length
            self.reprioritization_priority_frames = [None for _ in range(len(self.urls))]


    def selfcheck(self) -> bool:
        self._reprio_fix()  # TODO TEMP FIX
        # Make sure all field lists have the same length (will automatically check if a new list field is added)
        # Note: not ready for non-list types
        context_iter = iter(dataclasses.asdict(self).values())
        expected_length = len(next(context_iter))
        return all(len(i) == expected_length for i in context_iter)
    
    def get_experiment_setup(self, index: int):
        # Assume index is correct
        shallow_copy = dict((field.name, getattr(self, field.name)) for field in dataclasses.fields(self))
        return {(field[:-1] if len(field) > 1 else field) : values[index] for field, values in shallow_copy.items()}


async def main(
    configuration: QuicConfiguration,
    # urls: List[str],
    # headers: List[Dict], # MUST be of same length as URLs, with 1 headers enty per URL (can be None)
    # delays: List[float], # MUST be of same length as URLs, with 1 enty per URL (can be 0)
    experiment: ExperimentRequestContext,
    data: Optional[str],
    include: bool,
    output_dir: Optional[str],
    local_port: int,
    zero_rtt: bool,
) -> None:
    urls = experiment.urls

    # parse URL
    parsed = urlparse(urls[0])
    assert parsed.scheme in (
        "https",
        "wss",
    ), "Only https:// or wss:// URLs are supported."
    host = parsed.hostname
    if parsed.port is not None:
        port = parsed.port
    else:
        port = 443

    # check validity of 2nd urls and later.
    for i in range(1, len(urls)):
        _p = urlparse(urls[i])

        # fill in if empty
        _scheme = _p.scheme or parsed.scheme
        _host = _p.hostname or host
        _port = _p.port or port

        assert _scheme == parsed.scheme, "URL scheme doesn't match"
        assert _host == host, "URL hostname doesn't match"
        assert _port == port, "URL port doesn't match"

        # reconstruct url with new hostname and port
        _p = _p._replace(scheme=_scheme)
        _p = _p._replace(netloc="{}:{}".format(_host, _port))
        _p = urlparse(_p.geturl())
        urls[i] = _p.geturl()

    async with connect(
        host,
        port,
        configuration=configuration,
        create_protocol=HttpClient,
        session_ticket_handler=save_session_ticket,
        local_port=local_port,
        wait_connected=not zero_rtt,
    ) as client:
        client = cast(HttpClient, client)

        if parsed.scheme == "wss":
            ws = await client.websocket(urls[0], subprotocols=["chat", "superchat"])

            # send some messages and receive reply
            for i in range(2):
                message = "Hello {}, WebSocket!".format(i)
                print("> " + message)
                await ws.send(message)

                message = await ws.recv()
                print("< " + message)

            await ws.close()
        else:
            # perform request
            coros = [
                perform_http_request(
                    client=client,
                    experiment=experiment.get_experiment_setup(i),
                    # url=urls[i],
                    data=data,
                    include=include,
                    # delay_s=delays[i],
                    # headers=headers[i],
                    output_dir=output_dir,
                )
                for i in range(len(urls))
            ]
            await asyncio.gather(*coros)

            # process http pushes
            process_http_pushes(client=client, include=include, output_dir=output_dir)
        client._quic.close(error_code=ErrorCode.H3_NO_ERROR)


if __name__ == "__main__":
    defaults = QuicConfiguration(is_client=True)

    parser = argparse.ArgumentParser(description="HTTP/3 client")
    parser.add_argument(
        "url", type=str, nargs="+", help="the URL to query (must be HTTPS)"
    )
    parser.add_argument(
        "--experiment", type=str, help="Name of the (prioritization) experiment to run"
    )
    parser.add_argument(
        "--ca-certs", type=str, help="load CA certificates from the specified file"
    )
    parser.add_argument(
        "--cipher-suites",
        type=str,
        help="only advertise the given cipher suites, e.g. `AES_256_GCM_SHA384,CHACHA20_POLY1305_SHA256`",
    )
    parser.add_argument(
        "-d", "--data", type=str, help="send the specified data in a POST request"
    )
    parser.add_argument(
        "-i",
        "--include",
        action="store_true",
        help="include the HTTP response headers in the output",
    )
    parser.add_argument(
        "--max-data",
        type=int,
        help="connection-wide flow control limit (default: %d)" % defaults.max_data,
    )
    parser.add_argument(
        "--max-stream-data",
        type=int,
        help="per-stream flow control limit (default: %d)" % defaults.max_stream_data,
    )
    parser.add_argument(
        "-k",
        "--insecure",
        action="store_true",
        help="do not validate server certificate",
    )
    parser.add_argument("--legacy-http", action="store_true", help="use HTTP/0.9")
    parser.add_argument(
        "--output-dir",
        type=str,
        help="write downloaded files to this directory",
    )
    parser.add_argument(
        "-q",
        "--quic-log",
        type=str,
        help="log QUIC events to QLOG files in the specified directory",
    )
    parser.add_argument(
        "-l",
        "--secrets-log",
        type=str,
        help="log secrets to a file, for use with Wireshark",
    )
    parser.add_argument(
        "-s",
        "--session-ticket",
        type=str,
        help="read and write session ticket from the specified file",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="increase logging verbosity"
    )
    parser.add_argument(
        "--local-port",
        type=int,
        default=0,
        help="local port to bind for connections",
    )
    parser.add_argument(
        "--zero-rtt", action="store_true", help="try to send requests using 0-RTT"
    )

    parser.add_argument(
        "--dry-run", action="store_true", help="Check to make sure the provided experiment configuration will run (DEBUG purposes only)"
    )

    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        level=logging.DEBUG if args.verbose else logging.INFO,
    )

    if args.output_dir is not None and not os.path.isdir(args.output_dir):
        raise Exception("%s is not a directory" % args.output_dir)

    # prepare configuration
    configuration = QuicConfiguration(
        is_client=True, alpn_protocols=H0_ALPN if args.legacy_http else H3_ALPN
    )
    if args.ca_certs:
        configuration.load_verify_locations(args.ca_certs)
    if args.cipher_suites:
        configuration.cipher_suites = [
            CipherSuite[s] for s in args.cipher_suites.split(",")
        ]
    if args.insecure:
        configuration.verify_mode = ssl.CERT_NONE
    if args.max_data:
        configuration.max_data = args.max_data
    if args.max_stream_data:
        configuration.max_stream_data = args.max_stream_data
    if args.secrets_log:
        configuration.secrets_log_file = open(args.secrets_log, "a")
    if args.session_ticket:
        try:
            with open(args.session_ticket, "rb") as fp:
                configuration.session_ticket = pickle.load(fp)
        except FileNotFoundError:
            pass
    
    # prioritization experiments: user just passed in name of experiment, we set parameters here
    experiment = args.experiment
    request_count = 10
    delay_s = 0.05 # 200ms delay between subsequent requests (if part of the experiment)
    urls = []
    delays = []
    headers: List[RequestPriorityConfiguration] = []
    pre_request_prio_frames: List[RequestPriorityConfiguration] = []
    post_request_prio_frames: List[RequestPriorityConfiguration] = []
    reprioritization_frames: List[RequestPriorityConfiguration] | None = []

    mainURL = args.url[0] # should only ever be one anyway

    if not args.experiment:
        experiment = "no-priority-instant"

    logger.info("Running %s experiment on %i URLs %s", experiment, request_count, mainURL)

    ###
    # NO PRIORITIES
    ###

    # all requests issued at the same time, no priority information given
    if experiment == "no-priority-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)

    # requests issued staggered by delay_s seconds, no priority information given
    elif experiment == "no-priority-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)

    ###
    # Default urgency = 3 and incremental = true
    ###

    # Headers instant
    elif experiment == "u3-incremental-headers-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(RequestPriorityConfiguration(3, True))
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)

    # Priority frames immediately before the request, instant requests
    elif experiment == "u3-incremental-preframes-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(3, True))
            post_request_prio_frames.append(None)

    # Priority frames 100ms before the request, instant requests
    elif experiment == "u3-incremental-preframes-100ms-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(3, True, 0.1))
            post_request_prio_frames.append(None)

    # Priority frames 200ms before the request, instant requests
    elif experiment == "u3-incremental-preframes-200ms-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(3, True, 0.2))
            post_request_prio_frames.append(None)

    # Priority frames immediately after the request, instant requests
    elif experiment == "u3-incremental-postframes-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(3, True))

    # Priority frames 100ms after the request, instant requests
    elif experiment == "u3-incremental-postframes-100ms-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(3, True, 0.1))

    # Priority frames 200ms after the request, instant requests
    elif experiment == "u3-incremental-postframes-200ms-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(3, True, 0.2))

    # Headers staggered
    elif experiment == "u3-incremental-headers-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(RequestPriorityConfiguration(3, True))
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)

    # Priority frames immediately before the request, staggered requests
    elif experiment == "u3-incremental-preframes-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(3, True))
            post_request_prio_frames.append(None)

    # Priority frames 100ms before the request, staggered requests
    elif experiment == "u3-incremental-preframes-100ms-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(3, True, 0.1))
            post_request_prio_frames.append(None)

    # Priority frames 200ms before the request, staggered requests
    elif experiment == "u3-incremental-preframes-200ms-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(3, True, 0.2))
            post_request_prio_frames.append(None)

    # Priority frames immediately after the request, staggered requests
    elif experiment == "u3-incremental-postframes-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(3, True))

    # Priority frames 100ms after the request, staggered requests
    elif experiment == "u3-incremental-postframes-100ms-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(3, True, 0.1))


    # Priority frames 200ms after the request, staggered requests
    elif experiment == "u3-incremental-postframes-200ms-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(3, True, 0.2))

    ###
    # We give request 5 and 6 a much higher priority than the others, no incremental
    ###

    # Headers, instant requests
    elif experiment == "late-highprio-headers-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(RequestPriorityConfiguration(6))
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)

        headers[4].urgency = 0
        headers[5].urgency = 0

    # Priority frames immediately before the request, instant requests
    elif experiment == "late-highprio-preframes-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(6))
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4].urgency = 0
        pre_request_prio_frames[5].urgency = 0
    
    # Priority frames 100ms before the request, instant requests
    elif experiment == "late-highprio-preframes-100ms-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(6, False, 0.1))
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4].urgency = 0
        pre_request_prio_frames[5].urgency = 0

    # Priority frames 200ms before the request, instant requests
    elif experiment == "late-highprio-preframes-200ms-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(6, False, 0.2))
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4].urgency = 0
        pre_request_prio_frames[5].urgency = 0

    # Priority frames immediately after the request, instant requests
    elif experiment == "late-highprio-postframes-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(6))

        post_request_prio_frames[4].urgency = 0
        post_request_prio_frames[5].urgency = 0


    # Priority frames 100ms after the request, instant requests
    elif experiment == "late-highprio-postframes-100ms-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(6, False, 0.1))

        post_request_prio_frames[4].urgency = 0
        post_request_prio_frames[5].urgency = 0


    # Priority frames 200ms after the request, instant requests
    elif experiment == "late-highprio-postframes-200ms-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(6, False, 0.2))

        post_request_prio_frames[4].urgency = 0
        post_request_prio_frames[5].urgency = 0

    # Headers, staggered requests
    elif experiment == "late-highprio-headers-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(RequestPriorityConfiguration(6))
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)

        headers[4].urgency = 0
        headers[5].urgency = 0

    # Priority frames immediately before the request, staggered requests
    elif experiment == "late-highprio-preframes-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(6))
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4].urgency = 0
        pre_request_prio_frames[5].urgency = 0

    # Priority frames 100ms before the request, staggered requests
    elif experiment == "late-highprio-preframes-100ms-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(6, False, 0.1))
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4].urgency = 0
        pre_request_prio_frames[5].urgency = 0

    # Priority frames 200ms before the request, staggered requests
    elif experiment == "late-highprio-preframes-200ms-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(6, False, 0.2))
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4].urgency = 0
        pre_request_prio_frames[5].urgency = 0

    # Priority frames immediately after the request, staggered requests
    elif experiment == "late-highprio-postframes-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(6))

        post_request_prio_frames[4].urgency = 0
        post_request_prio_frames[5].urgency = 0

    # Priority frames 100ms after the request, staggered requests
    elif experiment == "late-highprio-postframes-100ms-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(6, False, 0.1))

        post_request_prio_frames[4].urgency = 0
        post_request_prio_frames[5].urgency = 0

    # Priority frames 200ms after the request, staggered requests
    elif experiment == "late-highprio-postframes-200ms-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(6, False, 0.2))

        post_request_prio_frames[4].urgency = 0
        post_request_prio_frames[5].urgency = 0


    ###
    # We give request 5 and 6 a much higher priority than the others + no incremental, other are incremental
    ###

    # Headers, instant requests
    elif experiment == "late-highprio-incremental-headers-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(RequestPriorityConfiguration(6, True))
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)

        headers[4].urgency = 0
        headers[4].incremental = False
        headers[5].urgency = 0
        headers[5].incremental = False

    # Priority frames immediately before the request, instant requests
    elif experiment == "late-highprio-incremental-preframes-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(6, True))
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4].urgency = 0
        pre_request_prio_frames[4].incremental = False
        pre_request_prio_frames[5].urgency = 0
        pre_request_prio_frames[5].incremental = False

    # Priority frames immediately before the request, instant requests
    elif experiment == "late-highprio-incremental-preframes-100ms-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(6, True, 0.1))
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4].urgency = 0
        pre_request_prio_frames[4].incremental = False
        pre_request_prio_frames[5].urgency = 0
        pre_request_prio_frames[5].incremental = False

    # Priority frames 200ms before the request, instant requests
    elif experiment == "late-highprio-incremental-preframes-200ms-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(6, True, 0.2))
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4].urgency = 0
        pre_request_prio_frames[4].incremental = False
        pre_request_prio_frames[5].urgency = 0
        pre_request_prio_frames[5].incremental = False

    # Priority frames immediately after the request, instant requests
    elif experiment == "late-highprio-incremental-postframes-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(6, True))

        post_request_prio_frames[4].urgency = 0
        post_request_prio_frames[4].incremental = False
        post_request_prio_frames[5].urgency = 0
        post_request_prio_frames[5].incremental = False

    # Priority frames 100ms after the request, instant requests
    elif experiment == "late-highprio-incremental-postframes-100ms-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(6, True, 0.1))

        post_request_prio_frames[4].urgency = 0
        post_request_prio_frames[4].incremental = False
        post_request_prio_frames[5].urgency = 0
        post_request_prio_frames[5].incremental = False

    # Priority frames 200ms after the request, instant requests
    elif experiment == "late-highprio-incremental-postframes-200ms-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(6, True, 0.2))

        post_request_prio_frames[4].urgency = 0
        post_request_prio_frames[4].incremental = False
        post_request_prio_frames[5].urgency = 0
        post_request_prio_frames[5].incremental = False

    # Headers, staggered requests
    elif experiment == "late-highprio-incremental-headers-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(RequestPriorityConfiguration(6, True))
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)

        headers[4].urgency = 0
        headers[4].incremental = False
        headers[5].urgency = 0
        headers[5].incremental = False

    # Priority frames immediately before the request, staggered requests
    elif experiment == "late-highprio-incremental-preframes-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(6, True))
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4].urgency = 0
        pre_request_prio_frames[4].incremental = False
        pre_request_prio_frames[5].urgency = 0
        pre_request_prio_frames[5].incremental = False

    # Priority frames 100ms before the request, staggered requests
    elif experiment == "late-highprio-incremental-preframes-100ms-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(6, True, 0.1))
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4].urgency = 0
        pre_request_prio_frames[4].incremental = False
        pre_request_prio_frames[5].urgency = 0
        pre_request_prio_frames[5].incremental = False

    # Priority frames 200ms before the request, staggered requests
    elif experiment == "late-highprio-incremental-preframes-200ms-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(6, True, 0.2))
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4].urgency = 0
        pre_request_prio_frames[4].incremental = False
        pre_request_prio_frames[5].urgency = 0
        pre_request_prio_frames[5].incremental = False

    # Priority frames immediately after the request, staggered requests
    elif experiment == "late-highprio-incremental-postframes-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(6, True))

        post_request_prio_frames[4].urgency = 0
        post_request_prio_frames[4].incremental = False
        post_request_prio_frames[5].urgency = 0
        post_request_prio_frames[5].incremental = False

    # Priority frames 100ms after the request, staggered requests
    elif experiment == "late-highprio-incremental-postframes-100ms-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(6, True, 0.1))

        post_request_prio_frames[4].urgency = 0
        post_request_prio_frames[4].incremental = False
        post_request_prio_frames[5].urgency = 0
        post_request_prio_frames[5].incremental = False

    # Priority frames 200ms after the request, staggered requests
    elif experiment == "late-highprio-incremental-postframes-200ms-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(6, True, 0.2))

        post_request_prio_frames[4].urgency = 0
        post_request_prio_frames[4].incremental = False
        post_request_prio_frames[5].urgency = 0
        post_request_prio_frames[5].incremental = False


    ###
    # All requests are in the same bucket incremental, except 5 and 6 are non incremental
    ###
        
    # Headers, instant
    elif experiment == "mixed-bucket-headers-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(RequestPriorityConfiguration(3, True))
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)

        headers[4].incremental = False
        headers[5].incremental = False

    # Priority frames immediately before the request
    elif experiment == "mixed-bucket-preframes-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(3, True))
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4].incremental = False
        pre_request_prio_frames[5].incremental = False

    # Priority frames 100ms before the request
    elif experiment == "mixed-bucket-preframes-100ms-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(3, True, 0.1))
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4].incremental = False
        pre_request_prio_frames[5].incremental = False

    # Priority frames 200ms before the request
    elif experiment == "mixed-bucket-preframes-200ms-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(3, True, 0.2))
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4].incremental = False
        pre_request_prio_frames[5].incremental = False

    # Priority frames immediately after the request
    elif experiment == "mixed-bucket-postframes-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(3, True))

        post_request_prio_frames[4].incremental = False
        post_request_prio_frames[5].incremental = False

    # Priority frames 100ms after the request
    elif experiment == "mixed-bucket-postframes-100ms-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(3, True, 0.1))

        post_request_prio_frames[4].incremental = False
        post_request_prio_frames[5].incremental = False

    # Priority frames 200ms after the request
    elif experiment == "mixed-bucket-postframes-200ms-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(3, True, 0.2))

        post_request_prio_frames[4].incremental = False
        post_request_prio_frames[5].incremental = False

    # Headers, staggered requests
    elif experiment == "mixed-bucket-headers-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(RequestPriorityConfiguration(3, True))
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)

        headers[4].incremental = False
        headers[5].incremental = False

    # Priority frames immediately before the request, staggered requests
    elif experiment == "mixed-bucket-preframes-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(3, True))
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4].incremental = False
        pre_request_prio_frames[5].incremental = False

    # Priority frames 100ms before the request, staggered requests
    elif experiment == "mixed-bucket-preframes-100ms-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(3, True, 0.1))
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4].incremental = False
        pre_request_prio_frames[5].incremental = False

    # Priority frames 200ms before the request, staggered requests
    elif experiment == "mixed-bucket-preframes-200ms-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(3, True, 0.2))
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4].incremental = False
        pre_request_prio_frames[5].incremental = False

    # Priority frames immediately after the request, staggered requests
    elif experiment == "mixed-bucket-postframes-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(3, True))

        post_request_prio_frames[4].incremental = False
        post_request_prio_frames[5].incremental = False

    # Priority frames 100ms after the request, staggered requests
    elif experiment == "mixed-bucket-postframes-100ms-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(3, True, 0.1))

        post_request_prio_frames[4].incremental = False
        post_request_prio_frames[5].incremental = False

    # Priority frames 200ms after the request, staggered requests
    elif experiment == "mixed-bucket-postframes-200ms-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(3, True, 0.2))

        post_request_prio_frames[4].incremental = False
        post_request_prio_frames[5].incremental = False

    ###
    # mixed-priority: We set all headers to u=5 and priority update frames for requests 5 and 6 to u=1
    ###
    # Priority frames immediately before the request
    elif experiment == "mixed-signals-preframes-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(RequestPriorityConfiguration(5, False))
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4] = RequestPriorityConfiguration(1, False)
        pre_request_prio_frames[5] = RequestPriorityConfiguration(1, False)

    # Priority frames 100ms before the request
    elif experiment == "mixed-signals-preframes-100ms-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(RequestPriorityConfiguration(5, False))
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4] = RequestPriorityConfiguration(1, False, 0.1)
        pre_request_prio_frames[5] = RequestPriorityConfiguration(1, False, 0.1)

    # Priority frames 200ms before the request
    elif experiment == "mixed-signals-preframes-200ms-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(RequestPriorityConfiguration(5, False))
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4] = RequestPriorityConfiguration(1, False, 0.2)
        pre_request_prio_frames[5] = RequestPriorityConfiguration(1, False, 0.2)

    # Priority frames immediately after the request
    elif experiment == "mixed-signals-postframes-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(RequestPriorityConfiguration(5, False))
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)

        post_request_prio_frames[4] = RequestPriorityConfiguration(1, False)
        post_request_prio_frames[5] = RequestPriorityConfiguration(1, False)

    # Priority frames 100ms after the request
    elif experiment == "mixed-signals-postframes-100ms-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(RequestPriorityConfiguration(5, False))
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)

        post_request_prio_frames[4] = RequestPriorityConfiguration(1, False, 0.1)
        post_request_prio_frames[5] = RequestPriorityConfiguration(1, False, 0.1)

    # Priority frames 200ms after the request
    elif experiment == "mixed-signals-postframes-200ms-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(RequestPriorityConfiguration(5, False))
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)
        
        post_request_prio_frames[4] = RequestPriorityConfiguration(1, False, 0.2)
        post_request_prio_frames[5] = RequestPriorityConfiguration(1, False, 0.2)

    # Priority frames immediately before the request, staggered requests
    elif experiment == "mixed-signals-preframes-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(RequestPriorityConfiguration(5, False))
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4] = RequestPriorityConfiguration(1, False)
        pre_request_prio_frames[5] = RequestPriorityConfiguration(1, False)

    # Priority frames 100ms before the request, staggered requests
    elif experiment == "mixed-signals-preframes-100ms-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(RequestPriorityConfiguration(5, False))
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4] = RequestPriorityConfiguration(1, False, 0.1)
        pre_request_prio_frames[5] = RequestPriorityConfiguration(1, False, 0.1)

    # Priority frames 200ms before the request, staggered requests
    elif experiment == "mixed-signals-preframes-200ms-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(RequestPriorityConfiguration(5, False))
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)

        pre_request_prio_frames[4] = RequestPriorityConfiguration(1, False, 0.2)
        pre_request_prio_frames[5] = RequestPriorityConfiguration(1, False, 0.2)


    # Priority frames immediately after the request, staggered requests
    elif experiment == "mixed-signals-postframes-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(RequestPriorityConfiguration(5, False))
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)

        post_request_prio_frames[4] = RequestPriorityConfiguration(1, False)
        post_request_prio_frames[5] = RequestPriorityConfiguration(1, False)

    # Priority frames 100ms after the request, staggered requests
    elif experiment == "mixed-signals-postframes-100ms-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(RequestPriorityConfiguration(5, False))
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)

        post_request_prio_frames[4] = RequestPriorityConfiguration(1, False, 0.1)
        post_request_prio_frames[5] = RequestPriorityConfiguration(1, False, 0.1)

    # Priority frames 200ms after the request, staggered requests
    elif experiment == "mixed-signals-postframes-200ms-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(RequestPriorityConfiguration(5, False))
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)

        post_request_prio_frames[4] = RequestPriorityConfiguration(1, False, 0.2)
        post_request_prio_frames[5] = RequestPriorityConfiguration(1, False, 0.2)

    ###
    # Reprioritization
    # We set the priority of all streams to 4, non incremental (something different from the default)
    # Streams 5 and 6 receive a reprioritization signal for urgency 1 after 50ms
    ###
        
    # Headers, instant
    elif experiment == "reprioritization-50ms-headers-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(RequestPriorityConfiguration(4, False))
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)
            reprioritization_frames.append(None)

        reprioritization_frames[4] = RequestPriorityConfiguration(1, False, 0.05)
        reprioritization_frames[5] = RequestPriorityConfiguration(1, False, 0.05)

    # Priority frames 20ms before the request
    elif experiment == "reprioritization-50ms-preframes-20ms-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(4, False, 0.02))
            post_request_prio_frames.append(None)
            reprioritization_frames.append(None)

        reprioritization_frames[4] = RequestPriorityConfiguration(1, False, 0.05)
        reprioritization_frames[5] = RequestPriorityConfiguration(1, False, 0.05)
    
    # Priority frames 20ms before the request
    elif experiment == "reprioritization-50ms-postframes-20ms-instant":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(0)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(4, False, 0.02))
            reprioritization_frames.append(None)

        reprioritization_frames[4] = RequestPriorityConfiguration(1, False, 0.05)
        reprioritization_frames[5] = RequestPriorityConfiguration(1, False, 0.05)

    # Headers, instant
    elif experiment == "reprioritization-50ms-headers-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(RequestPriorityConfiguration(4, False))
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(None)
            reprioritization_frames.append(None)

        reprioritization_frames[4] = RequestPriorityConfiguration(1, False, 0.05)
        reprioritization_frames[5] = RequestPriorityConfiguration(1, False, 0.05)

    # Priority frames 20ms before the request
    elif experiment == "reprioritization-50ms-preframes-20ms-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(RequestPriorityConfiguration(4, False, 0.02))
            post_request_prio_frames.append(None)
            reprioritization_frames.append(None)

        reprioritization_frames[4] = RequestPriorityConfiguration(1, False, 0.05)
        reprioritization_frames[5] = RequestPriorityConfiguration(1, False, 0.05)
    
    # Priority frames 20ms before the request
    elif experiment == "reprioritization-50ms-postframes-20ms-staggered":
        for i in range(request_count):
            urls.append(mainURL)
            delays.append(delay_s * i)
            headers.append(None)
            pre_request_prio_frames.append(None)
            post_request_prio_frames.append(RequestPriorityConfiguration(4, False, 0.02))
            reprioritization_frames.append(None)

        reprioritization_frames[4] = RequestPriorityConfiguration(1, False, 0.05)
        reprioritization_frames[5] = RequestPriorityConfiguration(1, False, 0.05)

    else:
        logger.error("Incorrect experiment set %s, quitting...", experiment)
        exit()

    # Reprio fix TODO TEMP
    reprioritization_frames = reprioritization_frames if len(reprioritization_frames) > 0 else None
    experiment_configuration = ExperimentRequestContext(
        urls=urls,
        delays=delays,
        request_priority_headers=headers,
        pre_request_priority_frames=pre_request_prio_frames,
        post_request_priority_frames=post_request_prio_frames,
        reprioritization_priority_frames=reprioritization_frames
    )
    assert experiment_configuration.selfcheck(), f"Potential misconfiguration for experiment [{experiment}]; received inconsistent list lengths."

    if args.quic_log:
        # configuration.quic_logger = QuicFileLogger(args.quic_log)
        # --quic-log should be a partial file path instead of a directory (so e.g., /srv/aioquic/qlog/PREFIX_)
        # the remainder of the filename is generated here based on the experiment name
        quic_log_path = args.quic_log + "_" + str(request_count) + ".qlog"

        configuration.quic_logger = QuicSingleFileLogger(quic_log_path)

    # headers = []
    # for url in args.url:
    #     headers.append( {"priority": "u=0, i"} )

    if args.dry_run:
        sys.exit(0)

    if uvloop is not None:
        uvloop.install()
    asyncio.run(
        main(
            configuration=configuration,
            # urls=urls,
            # headers=headers,
            # delays=delays,
            experiment=experiment_configuration,
            data=args.data,
            include=args.include,
            output_dir=args.output_dir,
            local_port=args.local_port,
            zero_rtt=args.zero_rtt,
        )
    )
