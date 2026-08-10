"""Helpers that read a host and a port from an endpoint string.
This is so it supports:

``host:port`` pair, or a ``scheme://host:port`` URL

An IPv6 host must be in brackets, for example ``[::1]:2209``.
"""

from urllib.parse import urlsplit, urlunsplit


def split_host_port(endpoint: str, port: int | None, default: int) -> tuple[str, int]:
    """Read a host and a port from a TCP endpoint.

    Args:
        endpoint: A host, a ``host:port`` pair, or a ``scheme://host:port``
            URL.
        port: An explicit port. It has priority over the port in the
            endpoint.
        default: The port to use when neither source gives one.

    Returns:
        The host and the resolved port.

    Raises:
        ValueError: If the endpoint has no host.
    """
    parts = urlsplit(endpoint if "://" in endpoint else f"//{endpoint}")

    if not parts.hostname:
        raise ValueError(f"No host in endpoint: {endpoint!r}")

    return parts.hostname, port or parts.port or default


def override_url_port(url: str, port: int | None) -> str:
    """Set the port of a SOAP URL when the caller gives one.

    The method keeps the URL as is when ``port`` is None. The URL, or the
    scheme, then sets the port.

    Args:
        url: The full URL of the SOAP service.
        port: An explicit port, or None to keep the port of the URL.

    Returns:
        The URL with the given port, or the URL as is.

    Raises:
        ValueError: If the URL has no host.
    """
    if port is None:
        return url

    parts = urlsplit(url)
    if not parts.hostname:
        raise ValueError(f"No host in URL: {url!r}")

    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return urlunsplit(parts._replace(netloc=f"{host}:{port}"))
