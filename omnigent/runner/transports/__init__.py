"""Transport factories for talking to a runner.

Server-to-runner control-plane traffic uses NATS request/reply. The UDS and
TCP modules remain focused transport mechanics for runner subprocess tests and
future deployment shapes.

| Transport     | Module                | Phase | Built-in?           |
|---------------|-----------------------|-------|---------------------|
| UDS           | uds.py                | 2     | ✓ httpx.AsyncHTTPTransport(uds=) |
| TCP           | tcp.py                | 3     | ✓ httpx.AsyncHTTPTransport |
| NATS          | nats_transport/       | 5     | ✗ custom code       |

Each module exposes a ``create_<transport>_client()`` factory plus
the wire-level pieces specific to the transport.
"""
