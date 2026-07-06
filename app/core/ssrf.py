# =============================================================================
# ficium-portal-api — SSRF guard for outbound webhooks
#
# Institutions register their own webhook URLs. Without validation, a
# malicious (or compromised) institution could point a webhook at an
# internal address and turn our server into an SSRF proxy — e.g.:
#   http://169.254.169.254/latest/meta-data/   (cloud metadata / creds)
#   http://localhost:6379                       (internal Redis)
#   http://10.x / 192.168.x / 172.16.x          (internal network)
#
# validate_webhook_url() is called at BOTH registration (reject early) and
# dispatch (defence-in-depth against DNS rebinding, where a hostname that
# resolved public at registration later resolves to a private IP).
# =============================================================================

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class WebhookUrlError(ValueError):
    """Raised when a webhook URL is not a safe, public HTTPS endpoint."""


# Extra hostnames that must never be targets regardless of resolution.
_BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata.google.internal",
    "metadata",
}


def _is_public_ip(ip_str: str) -> bool:
    """True only for globally-routable public unicast addresses."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    # Reject loopback, private, link-local (incl. 169.254.169.254 metadata),
    # multicast, reserved, unspecified. is_global covers most; we add explicit
    # checks so intent is obvious and future-proof.
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    return ip.is_global


def validate_webhook_url(url: str, *, require_https: bool = True) -> None:
    """
    Validate that `url` is a safe, public webhook endpoint.

    Raises WebhookUrlError if the URL is malformed, non-HTTP(S), points at a
    blocked hostname, or resolves to any non-public IP address.

    Resolves ALL A/AAAA records and rejects if ANY of them is private — a URL
    is only safe if every address it could reach is public.
    """
    parsed = urlparse(url)

    scheme = (parsed.scheme or "").lower()
    if require_https and scheme != "https":
        raise WebhookUrlError("Webhook URL must use https://.")
    if scheme not in ("http", "https"):
        raise WebhookUrlError("Webhook URL must be http(s).")

    host = parsed.hostname
    if not host:
        raise WebhookUrlError("Webhook URL has no host.")

    if host.lower() in _BLOCKED_HOSTNAMES:
        raise WebhookUrlError("Webhook URL host is not permitted.")

    # If the host is a literal IP, check it directly.
    try:
        ipaddress.ip_address(host)
        if not _is_public_ip(host):
            raise WebhookUrlError("Webhook URL resolves to a non-public address.")
        return
    except ValueError:
        pass  # not a literal IP — resolve it below

    # Resolve the hostname and reject if ANY resolved address is non-public.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise WebhookUrlError(f"Webhook URL host does not resolve: {host}") from e

    resolved = {str(info[4][0]) for info in infos}
    if not resolved:
        raise WebhookUrlError(f"Webhook URL host does not resolve: {host}")

    for ip_str in resolved:
        if not _is_public_ip(ip_str):
            raise WebhookUrlError(
                "Webhook URL resolves to a non-public address."
            )
