"""Process entry point: serve on one socket that accepts both IPv4 and IPv6.

``uvicorn --host ::`` goes through asyncio's ``create_server``, which sets IPV6_V6ONLY on the socket. The service then
answers on Fly's private IPv6 network but refuses IPv4, and Fly's health checks (and the image's own HEALTHCHECK on
127.0.0.1) connect over IPv4. Opening the socket here with IPV6_V6ONLY off serves both.
"""

import os
import socket

import uvicorn


def open_socket(host: str, port: int) -> socket.socket:
    if ":" in host:
        sock = socket.socket(socket.AF_INET6)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    else:
        sock = socket.socket(socket.AF_INET)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    return sock


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    server = uvicorn.Server(uvicorn.Config("app.main:app", host=host, port=port))
    server.run(sockets=[open_socket(host, port)])


if __name__ == "__main__":
    main()
