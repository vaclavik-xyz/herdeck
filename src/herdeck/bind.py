"""Listen-address policy shared by every network-facing herdeck service.

Kept dependency-free so the headless bridge can import it without the deck
stack (herdeck.app pulls in rendering)."""

from __future__ import annotations

import ipaddress
import os

UNSAFE_BIND_ENV = "HERDECK_ALLOW_UNSAFE_BIND"


def validate_bind(host: str, *, env_name: str, getenv=os.environ.get) -> str:
    """Return ``host`` when it is loopback or a Tailscale address (100.64.0.0/10
    or a ``*.ts.net`` name); raise ValueError naming ``env_name`` otherwise,
    unless ``HERDECK_ALLOW_UNSAFE_BIND=1`` explicitly overrides the policy."""
    if str(getenv(UNSAFE_BIND_ENV, "")).lower() in {"1", "true", "yes"}:
        return host
    if host == "localhost" or host.endswith(".ts.net"):
        return host
    message = (
        f"{env_name} must be loopback or a Tailscale address (got {host!r}); "
        f"set {UNSAFE_BIND_ENV}=1 to override"
    )
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(message) from exc
    if address.is_loopback or address in ipaddress.ip_network("100.64.0.0/10"):
        return host
    raise ValueError(message)
