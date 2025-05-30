This repo is based on a fork (https://github.com/http3-prioritization/aoiquic) of the aiortc/aioquic project (https://github.com/aioquic) that implements QUIC and HTTP/3 into python.

This repo includes changes and additions to the fork.

Points of interest:
- HTTP/3 client script: trunk/examples/http3_client.py

- .qlog to .sqlog converter: trunk/scripts/json_to_jsonseq.py



Usage of the http/3 client:
- sudo python3 trunk/examples/http3_client.py --include --quic-log ./logfiles [URL]


Example - communicate with local quiche server:
- sudo python3 trunk/examples/http3_client.py --include --quic-log ./logfiles https://127.0.0.1:4433 --ca-certs ../mycert.crt --test-env True



Usage of the .qlog to .sqlog converter:
- python3 trunk/scripts/json_to_jsonseq.py [qlog file] [new sqlog file]
