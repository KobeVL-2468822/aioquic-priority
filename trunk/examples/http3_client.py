import argparse
import asyncio
import logging
import os
import pickle
import ssl
import time
from collections import deque
from typing import BinaryIO, Callable, Deque, Dict, List, Optional, Union, cast
from urllib.parse import urlparse
from bs4 import BeautifulSoup
import re
import struct

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
from aioquic.quic.logger import QuicFileLogger
from aioquic.tls import CipherSuite, SessionTicket
from urllib.parse import urljoin
try:
    import uvloop
except ImportError:
    uvloop = None






from dataclasses import dataclass


@dataclass
class PriorityUpdateParameters:
    type: Optional[int] = None
    length: Optional[int] = None
    prioritized_element_id: Optional[int] = None
    priority_field_value: Optional[int] = None




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
        self._quic_logger: Optional[QuicLoggerTrace] = self._quic._quic_logger

        if self._quic.configuration.alpn_protocols[0].startswith("hq-"):
            self._http = H0Connection(self._quic)
        else:
            self._http = H3Connection(self._quic)

    async def get(self, url: str, headers: Optional[Dict] = None) -> Deque[H3Event]:
        """
        Perform a GET request.
        """
        return await self._request(
            HttpRequest(method="GET", url=URL(url), headers=headers)
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

    async def _request(self, request: HttpRequest) -> Deque[H3Event]:
        stream_id = self._quic.get_next_available_stream_id()
        self._http.send_headers(
            stream_id=stream_id,
            headers=[
                (b":method", request.method.encode()),
                (b":scheme", request.url.scheme.encode()),
                (b":authority", request.url.authority.encode()),
                (b":path", request.url.full_path.encode()),
                (b"user-agent", USER_AGENT.encode()),
            ]
            + [(k.encode(), v.encode()) for (k, v) in request.headers.items()],
            end_stream = not request.content, #False if request.method == "GET" else not request.content
        )
        if request.content:
            self._http.send_data(
                stream_id=stream_id, data=request.content, end_stream = True #False if request.method == "GET" else not request.content
            )

        waiter = self._loop.create_future()
        self._request_events[stream_id] = deque()
        self._request_waiter[stream_id] = waiter
        self.transmit()

        return await asyncio.shield(waiter)


    def send_priority_update(self, control_stream: int, stream_id: int, new_priority: int, incremental: bool) -> None:
        priority_string = f"u={new_priority}"
        if(incremental):
            priority_string = f"u={new_priority}, i"
        frame_bytes, parameters = self.build_priority_update_frame(stream_id, priority_string)
        self._quic.send_stream_data(control_stream, frame_bytes, False)

        # log event
        if self._quic_logger is not None:
            self._quic_logger.log_event(
                category="http",
                event="priority_updated",
                data=self.set_priority_update_parameters_log(
                    owner="local", parameters=parameters
                ),
            )

    # async version oof send_priority_update
    async def _send_priority_update(self, control_stream: int, stream_id: int, new_priority: int, incremental: bool, sleepTime: int) -> bool:
        print(f"SEND PRIORIYY UPDATE - start timer")
        time.sleep(sleepTime)
        print(f"SEND PRIORIYY UPDATE - send")

        self.send_priority_update(stream_id, new_priority)
        return True


    # when logging a PRIORITY_UPDATE frame, all parameters will be shown correctly
    def set_priority_update_parameters_log(
        self, owner: str, parameters: PriorityUpdateParameters):
        data = {
            "owner": owner,
            "type": parameters.type,
            "length": parameters.type,
            "prioritized_element_id": parameters.prioritized_element_id,
            "priority_field_value": parameters.priority_field_value
        }
        return data

    # used to build the PRIORITY_UPDATE frame
    def encode_varint(self, value):
        """ Encode an integer using QUIC variable-length encoding. """
        if value < 0x40:
            return struct.pack("!B", value)
        elif value < 0x4000:
            return struct.pack("!H", (value | 0x4000))
        elif value < 0x40000000:
            return struct.pack("!I", (value | 0x80000000))
        else:
            return struct.pack("!Q", (value | 0xC000000000000000))


    # PRIORITY_UPDATE frame is not supported by vanilla aioquic -> implement ourselves
    def build_priority_update_frame(self, prioritized_element_id, priority_field_value):
        frame_type=0xF0700
    
        frame_type_encoded = self.encode_varint(frame_type)
        prioritized_element_encoded = self.encode_varint(prioritized_element_id)
        priority_field_value_bytes = priority_field_value.encode("utf-8")  # Convert string to bytes

        length = len(prioritized_element_encoded) + len(priority_field_value_bytes)
        length_encoded = self.encode_varint(length)

        parameters = PriorityUpdateParameters(
            type= frame_type,
            length= length,
            prioritized_element_id= prioritized_element_id,
            priority_field_value= priority_field_value
        )

        return frame_type_encoded + length_encoded + prioritized_element_encoded + priority_field_value_bytes, parameters


# determine urgency of stream based on requested file extension
def determine_urgency(resource_url: str) -> int:
    ext = os.path.splitext(urlparse(resource_url).path)[1].lower()
    
    urgency_mapping = {
        ".html": 0,
        ".htm": 0,
        ".json": 1,
        ".js": 1,
        ".css": 2,
        ".mp4": 5,
        ".avi": 5,
        ".mov": 5,
        ".webm": 5,
        ".mp3": 4,
        ".wav": 4,
        ".ogg": 4,
    }
    return urgency_mapping.get(ext, 3) #Default urgency value -> 3


# determine incremental value of stream based on requested file extension
def determine_incremental(resource_url: str) -> int:
    ext = os.path.splitext(urlparse(resource_url).path)[1].lower()
    
    incremental_mapping = {
        ".html": True,
        ".htm": True,
        ".json": True,
        ".css": False,
        ".js": False,
        ".mp4": True,
        ".avi": True,
        ".mov": True,
        ".webm": True,
        ".mp3": True,
        ".wav": True,
        ".ogg": True,
    }

    return incremental_mapping.get(ext, False) #Default incremental value -> False


async def perform_complete_http_request(client: HttpClient, url: str, data: Optional[str], include: bool, output_dir: Optional[str]) -> None:
    start = time.time()
    if data is not None:
        data_bytes = data.encode()
        http_events = await client.post(url, data=data_bytes, headers={
            "content-length": str(len(data_bytes)),
            "content-type": "application/x-www-form-urlencoded",
            "priority": "u=0, i",
        })
        method = "POST"
    else:
        http_events = await client.get(url, headers={"priority": "u=0, i"})
        method = "GET"
    elapsed = time.time() - start

    # Print speed
    octets = 0
    for http_event in http_events:
        if isinstance(http_event, DataReceived):
            octets += len(http_event.data)
    logger.info(
        "Response received for %s %s : %d bytes in %.1f s (%.3f Mbps)"
        % (method, urlparse(url).path, octets, elapsed, octets * 8 / elapsed / 1000000)
    )

    # Output response
    html_content = b"".join([event.data for event in http_events if isinstance(event, DataReceived)])
    html_str = html_content.decode("utf-8", errors="ignore")

    resources = extract_resources(html_str, url)

    tasks = []

    # Fetch all resources
    for resource_url in resources:
        urgency = determine_urgency(resource_url)
        incremental = determine_incremental(resource_url)
        logger.info(f"Fetching resource: {resource_url} with urgency {urgency} - incremental: {incremental}")
        if incremental:
            task = asyncio.create_task(client.get(resource_url,  headers={"priority": f"u={urgency},i"})) 
            tasks.append(task)
        else:
            task = asyncio.create_task(client.get(resource_url,  headers={"priority": f"u={urgency}"}))
            tasks.append(task)

    
    responses = await asyncio.gather(*tasks, return_exceptions=True)

    # Save the main page
    if output_dir is not None:
        output_path = os.path.join(output_dir, os.path.basename(urlparse(url).path) or "index.html")
        with open(output_path, "wb") as output_file:
            write_response(http_events=http_events, include=include, output_file=output_file)




# Controlled testing
async def perform_complete_http_request_to_quiche(client: HttpClient, url: str, data: Optional[str], 
    include: bool, output_dir: Optional[str]) -> None:
    
    http_events = await client.get(url, headers={"priority": "u=0, i"})
    # Get these specific files from the quiche server
    resources = [
        "http://localhost/docs/100mbitFile.bin",
        "http://localhost/docs/100mbitFile2.bin",
        "http://localhost/docs/50mbitFile.bin",
        "http://localhost/docs/60mbitFile.bin",
    ]

    # send priority update frames as well
    SEND_PRIORITY_UPDATE_FRAMES = True
    # pre determined priority header information
    urgencies = [5, 2, 1, 4]
    incrementals = [False, False, False, False]

    tasks = []
    counter = 0

    # Fetch all resources
    for resource_url in resources:
        incremental = incrementals[counter]
        urgency = urgencies[counter]
        counter = counter+1
        logger.info(f"Fetching resource: {url} with urgency {urgency} - incremental: {incremental}")
        if incremental:
            task = asyncio.create_task(client.get(resource_url,  headers={"priority": f"u={urgency},i"})) 
            tasks.append(task)
        else:
            task = asyncio.create_task(client.get(resource_url,  headers={"priority": f"u={urgency}"})) 
            tasks.append(task)


    if (SEND_PRIORITY_UPDATE_FRAMES):
        CONTROL_STREAM_ID = 2       # In test environment the control stream id = 2
        # In test environment the requested resources will be requested on streams 
        #that can be divided by 4 with no residual value (excl. 0) -> 4, 8, 12, ...
        client.send_priority_update(CONTROL_STREAM_ID, 4, urgencies[0], incrementals[0])
        client.send_priority_update(CONTROL_STREAM_ID, 8, urgencies[1], incrementals[1])
        client.send_priority_update(CONTROL_STREAM_ID, 12, urgencies[2], incrementals[2])
        client.send_priority_update(CONTROL_STREAM_ID, 16, urgencies[3], incrementals[3])
    
    responses = await asyncio.gather(*tasks, return_exceptions=True)

    
    # Save the main page
    if output_dir is not None:
        output_path = os.path.join(output_dir, os.path.basename(urlparse(url).path) or "index.html")
        with open(output_path, "wb") as output_file:
            write_response(http_events=http_events, include=include, output_file=output_file)




async def perform_http_request(
    client: HttpClient,
    url: str,
    data: Optional[str],
    include: bool,
    output_dir: Optional[str],
) -> None:
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
        http_events = await client.get(url)
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



def extract_resources(html: str, base_url: str) -> List[str]:
    """
    Extract resources (images, css, js, etc.) from the given HTML.
    """
    soup = BeautifulSoup(html, 'html.parser')
    resources = set()

    # Extract images, links to CSS, JavaScript, etc.
    for img in soup.find_all('img', src=True):
        resources.add(urljoin(base_url, img['src']))
    for link in soup.find_all('link', href=True):
        resources.add(urljoin(base_url, link['href']))
    for script in soup.find_all('script', src=True):
        resources.add(urljoin(base_url, script['src']))
    for video in soup.find_all('video', src=True):
        resources.add(urljoin(base_url, video['src']))
    for audio in soup.find_all('audio', src=True):
        resources.add(urljoin(base_url, audio['src']))
    

    return list(resources)



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


async def main(
    configuration: QuicConfiguration,
    urls: List[str],
    data: Optional[str],
    include: bool,
    output_dir: Optional[str],
    local_port: int,
    zero_rtt: bool,
    test_env: bool,
) -> None:
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
            if (test_env):
                # perform request
                coros = [
                    perform_complete_http_request_to_quiche(
                        client=client,
                        url=url,
                        data=data,
                        include=include,
                        output_dir=output_dir,
                    )
                    for url in urls
                ]
                await asyncio.gather(*coros)
                # process http pushes
                process_http_pushes(client=client, include=include, output_dir=output_dir)
            else:
                # perform request
                coros = [
                    perform_complete_http_request(
                        client=client,
                        url=url,
                        data=data,
                        include=include,
                        output_dir=output_dir,
                    )
                    for url in urls
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
        "--test-env", type=bool, default=False, help="execute with predetermined test request data"
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
    if args.quic_log:
        configuration.quic_logger = QuicFileLogger(args.quic_log)
    if args.secrets_log:
        configuration.secrets_log_file = open(args.secrets_log, "a")
    if args.session_ticket:
        try:
            with open(args.session_ticket, "rb") as fp:
                configuration.session_ticket = pickle.load(fp)
        except FileNotFoundError:
            pass

    if uvloop is not None:
        uvloop.install()
    asyncio.run(
        main(
            configuration=configuration,
            urls=args.url,
            data=args.data,
            include=args.include,
            output_dir=args.output_dir,
            local_port=args.local_port,
            zero_rtt=args.zero_rtt,
            test_env=args.test_env,
        )
    )
