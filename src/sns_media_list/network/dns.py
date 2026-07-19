"""Public-address DNS validation and destination pinning."""

import ipaddress
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..errors import AppError

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Sequence[IPAddress]]


def resolve_system(hostname: str, port: int) -> Sequence[IPAddress]:
    """Resolve all A and AAAA answers using the operating system resolver."""
    try:
        results = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise AppError(
            "unsafe_destination", "The upstream destination could not be resolved."
        ) from error

    addresses: list[IPAddress] = []
    for _family, _kind, _protocol, _canonname, sockaddr in results:
        addresses.append(ipaddress.ip_address(sockaddr[0]))
    return tuple(dict.fromkeys(addresses))


@dataclass(frozen=True, slots=True)
class ValidatedTarget:
    """Represent a hostname pinned to one validated public IP address."""

    hostname: str
    port: int
    address: IPAddress


@dataclass(frozen=True, slots=True)
class DestinationPolicy:
    """Validate exact host allowlists and pin destinations before connecting."""

    allowed_hosts: frozenset[str]
    resolver: Resolver = resolve_system

    def validate(self, hostname: str, port: int) -> ValidatedTarget:
        """Validate a host and return a selected public address for it."""
        normalized = hostname.lower().rstrip(".")
        if normalized not in self.allowed_hosts or port != 443:
            raise AppError("unsafe_destination", "The upstream destination is not allowed.")

        addresses = list(self.resolver(normalized, port))
        if not addresses or any(not address.is_global for address in addresses):
            raise AppError("unsafe_destination", "The upstream destination is not public.")
        return ValidatedTarget(normalized, port, addresses[0])
