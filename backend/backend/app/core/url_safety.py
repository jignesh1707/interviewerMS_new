"""Outbound URL validation for webhook callbacks (SSRF protection)."""

import ipaddress
import socket
from urllib.parse import urlparse

from app.config import get_settings
from app.core.errors import ValidationAppError


def _blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def validate_callback_url(url: str, *, resolve: bool = True) -> str:
    """Return the url if it is a safe webhook target, else raise ValidationAppError.

    Requires https (unless CALLBACK_ALLOW_INSECURE), no embedded credentials, an optional
    host allowlist (CALLBACK_ALLOWED_HOSTS), and that every resolved address is public.
    """
    settings = get_settings()
    value = (url or "").strip()
    parsed = urlparse(value)
    allowed_schemes = {"https", "http"} if settings.callback_allow_insecure else {"https"}
    if parsed.scheme not in allowed_schemes:
        raise ValidationAppError("callback_url must use https")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValidationAppError("callback_url has no host")
    if parsed.username or parsed.password:
        raise ValidationAppError("callback_url must not contain credentials")
    allowlist = settings.callback_host_set
    if allowlist and host not in allowlist:
        raise ValidationAppError("callback_url host is not in the allowed callback hosts")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _blocked(literal) and not settings.callback_allow_insecure:
            raise ValidationAppError("callback_url must not target a private or internal address")
        return value

    if resolve and not settings.callback_allow_insecure:
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (socket.gaierror, ValueError) as exc:
            raise ValidationAppError("callback_url host could not be resolved") from exc
        for info in infos:
            if _blocked(ipaddress.ip_address(info[4][0])):
                raise ValidationAppError("callback_url must not target a private or internal address")
    return value
