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
UPDATE_CHECK_NAME = "mikrotik-push-update-check"
# The API user (read,api) cannot see the check's result (RouterOS 7.25 answers it "finding out latest
# version..." forever), so the task that checks also writes the result into its own comment.
UPDATE_CHECK_COMMAND = (
    '/system package update check-for-updates; :local s ""; '
    ':for i from=1 to=60 do={ :set s [/system package update get status]; '
    ':if ([:pick $s ([:len $s] - 3) [:len $s]] = "...") do={ :delay 1s } }; '
    '/system scheduler set [find name="mikrotik-push-update-check"] '
    'comment=("Home Assistant (MikroTik Push): latest=" . [/system package update get latest-version] '
    '. "; status=" . $s)'
)
COMMENT = "Home Assistant (MikroTik Push)"
POLICY = "read,api"

_HOSTNAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*")
_PLAIN = re.compile(r"[A-Za-z0-9_-]+")


def generate_password() -> str:
    return secrets.token_urlsafe(18)


def is_safe_host(host: str) -> bool:
    """Hosts end up inside a script the user pastes into their router: only IPs and DNS names."""
    return bool(_HOSTNAME.fullmatch(host)) or is_ip(host)


def is_ip(host: str) -> bool:
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
            f'key-usage=key-cert-sign,crl-sign; /certificate sign "{CA_NAME}"; :delay 5s; {_wait_for_key(CA_NAME)}; '
            f'/certificate add name="{CERTIFICATE_NAME}" common-name="{host}" days-valid=3650 '
            f'key-usage=tls-server; /certificate sign "{CERTIFICATE_NAME}" ca="{CA_NAME}"; :delay 5s; '
            f'{_wait_for_key(CERTIFICATE_NAME)}; '
            f'/ip service set api-ssl certificate="{CERTIFICATE_NAME}" }}',
            "/ip service set api-ssl disabled=no",
            "# The router only learns of new RouterOS versions when told to look; do it daily.",
            f':if ([:len [/system scheduler find name="{UPDATE_CHECK_NAME}"]] = 0) do={{ '
            f'/system scheduler add name="{UPDATE_CHECK_NAME}" interval=1d comment="{COMMENT}" }}',
            f'/system scheduler set [find name="{UPDATE_CHECK_NAME}"] policy=read,write,test '
            f'on-event="{_quoted(UPDATE_CHECK_COMMAND)}"',
            f":do {{ {UPDATE_CHECK_COMMAND} }} on-error={{}}",
        ]
    )


def _quoted(code: str) -> str:
    """Code inside a RouterOS double-quoted string: `$` would be expanded now, not when the task runs."""
    return code.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")


def _wait_for_key(name: str) -> str:
    """Signing takes longer on a slow CPU than any fixed delay: wait up to a minute for the key."""
    return (
        f':for tries from=1 to=60 do={{ '
        f':if ([:len [/certificate find name="{name}" private-key=yes]] = 0) do={{ :delay 1s }} }}'
    )


def _single_host_network(address: str) -> str:
    ip = ipaddress.ip_address(address)
    return f"{ip}/{ip.max_prefixlen}"
