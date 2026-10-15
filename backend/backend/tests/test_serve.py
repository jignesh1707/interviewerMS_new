import socket

import pytest

from app.serve import open_socket


def _connect(family: int, address: str, port: int) -> None:
    with socket.socket(family) as client:
        client.settimeout(2)
        client.connect((address, port))


def test_ipv6_wildcard_socket_also_accepts_ipv4():
    try:
        sock = open_socket("::", 0)
    except OSError:
        pytest.skip("no IPv6 on this machine")
    with sock:
        sock.listen(2)
        port = sock.getsockname()[1]
        _connect(socket.AF_INET6, "::1", port)
        _connect(socket.AF_INET, "127.0.0.1", port)


def test_ipv4_host_gives_an_ipv4_socket():
    with open_socket("127.0.0.1", 0) as sock:
        assert sock.family == socket.AF_INET
