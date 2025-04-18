Custom aioquic for HTTP/3 prioritization testing
================================================

This version of aioquic adds additional logging for the RFC 9218 prioritization signals and frames.

Installation instructions that worked for me:

.. code-block:: console

    git clone https://github.com/http3-prioritization/aioquic trunk
    git fetch
    git checkout priority-logging

    sudo apt install -y libssl-dev python3-dev python3-pip

    cd trunk
    pip3 install -e .
    pip3 install asgiref dnslib "flask<2.2" httpbin starlette "werkzeug<2.1" wsproto


You can then run the example server with:

.. code-block:: console

    cd trunk
    python3 examples/http3_server.py --port 443 --certificate /etc/letsencrypt/live/your.domain.com/fullchain.pem --private-key /etc/letsencrypt/live/your.domain.com/privkey.pem --verbose --quic-log ../server-qlogs/


Note: for proper browser interop, you should use port 443 and a real (letsencrypt) TLS certificate for the actual domain you're running the server on. Local testing is possible, [but annoying](https://github.com/aiortc/aioquic/tree/main/examples#chromium-and-chrome-usage).


You can then verify the basic setup is working using curl:

.. code-block:: console
    
    docker run -it --rm rmarx/curl-http3 curl -IL https://your.domain.com --http3 --connect-timeout 2 -H "priority: u=5, i"


Note: for proper browser interop, you also need to run an HTTP/2 (or HTTP/1.1) server that sends the correct alt-svc indicator.

My HTTP/2 setup for apache::

    <VirtualHost *:443>
        ServerAdmin rmarx@akamai.com
        ServerName your.domain.com
        DocumentRoot /var/www
        ErrorLog ${APACHE_LOG_DIR}/error.log
        CustomLog ${APACHE_LOG_DIR}/access.log combined

        Header set alt-svc "h3=\":443\"; ma=86400"

        Include /etc/letsencrypt/options-ssl-apache.conf
        SSLCertificateFile /etc/letsencrypt/live/your.domain.com/fullchain.pem
        SSLCertificateKeyFile /etc/letsencrypt/live/your.domain.com/privkey.pem
    </VirtualHost>


Note: Chromium and Firefox will switch to HTTP/3 ASAP after receiving the alt-svc, but Safari is usually slower (can wait until the HTTP/2 connection times out until it tries HTTP/3). 
The fastes way for testing I've found is load a page over HTTP/2 once in each browser, close the browsers to force close the HTTP/2 connection (alt-svc info will stay cached), 
and then open the browsers again after a few seconds. This should lead to consistent HTTP/3 usage. 

"Live" qlog support
-------------------

This version of aioquic support live retrieval of the "current" connection's qlog output.

Hit the /qlog or /qlog.json endpoints (e.g., https://www.example.org/qlog.json) and you'll get a qlog JSON string back for the current connection, including the request for the qlog file.

This can for example be used for live debugging inside a browser or for easy qlog extraction without access to the server (or having to wait until the connection is closed for the actual .qlog file to be written to disk).

What is ``aioquic``?
--------------------

``aioquic`` is a library for the QUIC network protocol in Python. It features
a minimal TLS 1.3 implementation, a QUIC stack and an HTTP/3 stack.

QUIC was standardised in `RFC 9000`_ and HTTP/3 in `RFC 9114`_.
``aioquic`` is regularly tested for interoperability against other
`QUIC implementations`_.

To learn more about ``aioquic`` please `read the documentation`_.

Why should I use ``aioquic``?
-----------------------------

``aioquic`` has been designed to be embedded into Python client and server
libraries wishing to support QUIC and / or HTTP/3. The goal is to provide a
common codebase for Python libraries in the hope of avoiding duplicated effort.

Both the QUIC and the HTTP/3 APIs follow the "bring your own I/O" pattern,
leaving actual I/O operations to the API user. This approach has a number of
advantages including making the code testable and allowing integration with
different concurrency models.

Features
--------

- QUIC stack conforming with `RFC 9000`_
- HTTP/3 stack conforming with `RFC 9114`_
- minimal TLS 1.3 implementation conforming with `RFC 8446`_
- IPv4 and IPv6 support
- connection migration and NAT rebinding
- logging TLS traffic secrets
- logging QUIC events in QLOG format
- HTTP/3 server push support

Requirements
------------

``aioquic`` requires Python 3.7 or better, and the OpenSSL development headers.

Linux
.....

On Debian/Ubuntu run:

.. code-block:: console

   $ sudo apt install libssl-dev python3-dev

On Alpine Linux run:

.. code-block:: console

   $ sudo apk add openssl-dev python3-dev bsd-compat-headers libffi-dev

OS X
....

On OS X run:

.. code-block:: console

   $ brew install openssl

You will need to set some environment variables to link against OpenSSL:

.. code-block:: console

   $ export CFLAGS=-I/usr/local/opt/openssl/include
   $ export LDFLAGS=-L/usr/local/opt/openssl/lib

Windows
.......

On Windows the easiest way to install OpenSSL is to use `Chocolatey`_.

.. code-block:: console

   > choco install openssl

You will need to set some environment variables to link against OpenSSL:

.. code-block:: console

  > $Env:INCLUDE = "C:\Progra~1\OpenSSL-Win64\include"
  > $Env:LIB = "C:\Progra~1\OpenSSL-Win64\lib"

Running the examples
--------------------

`aioquic` comes with a number of examples illustrating various QUIC usecases.

You can browse these examples here: https://github.com/aiortc/aioquic/tree/main/examples

License
-------

``aioquic`` is released under the `BSD license`_.

.. _read the documentation: https://aioquic.readthedocs.io/en/latest/
.. _QUIC implementations: https://github.com/quicwg/base-drafts/wiki/Implementations
.. _cryptography: https://cryptography.io/
.. _Chocolatey: https://chocolatey.org/
.. _BSD license: https://aioquic.readthedocs.io/en/latest/license.html
.. _RFC 8446: https://datatracker.ietf.org/doc/html/rfc8446
.. _RFC 9000: https://datatracker.ietf.org/doc/html/rfc9000
.. _RFC 9114: https://datatracker.ietf.org/doc/html/rfc9114
