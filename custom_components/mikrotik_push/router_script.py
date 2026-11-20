"""Generate the RouterOS commands that prepare a router for this integration.

The user pastes the result into a RouterOS terminal. It creates a read-only API user with a
one-time password, so nothing has to be typed or remembered.
"""

from __future__ import annotations

import ipaddress
import re
import secrets

DEFAULT_USERNAME = "mikrotik-push"
CERTIFICATE_NAME = "mikrotik-push-api"
CA_NAME = "mikrotik-push-ca"
COMMENT = "Home Assistant (MikroTik Push)"
POLICY = "read,api"

_HOSTNAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*")
_PLAIN = re.compile(r"[A-Za-z0-9_-]+")


def generate_password() -> str:
    return secrets.token_urlsafe(18)


def is_safe_host(host: str) -> bool:
    """Hosts end up inside a script the user pastes into their router: only IPs and DNS names."""
    if _HOSTNAME.fullmatch(host):
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def is_plain(value: str) -> bool:
    """User names and passwords go inside quotes: allow only characters with no meaning there."""
    return bool(_PLAIN.fullmatch(value))


def build_setup_script(
    *, host: str, ha_address: str | None, password: str, username: str = DEFAULT_USERNAME
) -> str:
    """RouterOS script: group (read + api only), user, certificate and api-ssl service.

    Safe to paste again: the existing group and user are updated, so the password always matches.
    """
    if not is_safe_host(host):
        raise ValueError(f"unsafe host: {host!r}")
    if not (is_plain(username) and is_plain(password)):
        raise ValueError("user name and password may only contain letters, digits, '-' and '_'")
    only_from = f" address={_single_host_network(ha_address)}" if ha_address else ""

    return "\n".join(
        [
            f"# {COMMENT}: read-only API user. Review, then paste into a RouterOS terminal.",
            f':if ([:len [/user group find name="{username}"]] = 0) do={{ '
            f'/user group add name="{username}" policy={POLICY} comment="{COMMENT}" }} else={{ '
            f'/user group set [/user group find name="{username}"] policy={POLICY} }}',
            f':if ([:len [/user find name="{username}"]] = 0) do={{ '
            f'/user add name="{username}" group="{username}" password="{password}"{only_from} '
            f'comment="{COMMENT}" }} else={{ '
            f'/user set [/user find name="{username}"] group="{username}" '
            f'password="{password}"{only_from} }}',
            "# api-ssl needs a certificate: a local CA signs one for this router (skipped once it exists).",
            f':if ([:len [/certificate find name="{CERTIFICATE_NAME}" private-key=yes]] = 0) do={{ '
            f'/certificate remove [find name="{CERTIFICATE_NAME}"]; '
            f'/certificate remove [find name="{CA_NAME}"]; '
            f'/certificate add name="{CA_NAME}" common-name="{CA_NAME}" days-valid=3650 '
            f'key-usage=key-cert-sign,crl-sign; /certificate sign "{CA_NAME}"; :delay 5s; '
            f'/certificate add name="{CERTIFICATE_NAME}" common-name="{host}" days-valid=3650 '
            f'key-usage=tls-server; /certificate sign "{CERTIFICATE_NAME}" ca="{CA_NAME}"; :delay 5s; '
            f'/ip service set api-ssl certificate="{CERTIFICATE_NAME}" }}',
            "/ip service set api-ssl disabled=no",
        ]
    )


def _single_host_network(address: str) -> str:
    ip = ipaddress.ip_address(address)
    return f"{ip}/{ip.max_prefixlen}"
