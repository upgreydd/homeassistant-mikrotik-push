"""What the router says about itself once per connection: model, version, memory, uptime."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .routeros import ConnectionLost, RouterOSConnection, RouterOSError

_UNIT_SECONDS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
_UPTIME_PART = re.compile(r"(\d+)([wdhms])")


@dataclass(frozen=True, slots=True)
class RouterInfo:
    name: str
    model: str | None
    serial_number: str | None
    version: str | None


def parse_uptime(text: str) -> int:
    """RouterOS uptime such as `1w5d6h13m16s` in seconds."""
    return sum(int(number) * _UNIT_SECONDS[unit] for number, unit in _UPTIME_PART.findall(text))


async def read_router_info(connection: RouterOSConnection) -> RouterInfo:
    identity = (await connection.call("/system/identity/print"))[0]
    resource = (await connection.call("/system/resource/print"))[0]
    board = await _routerboard(connection)
    return RouterInfo(
        name=identity["name"],
        model=board.get("model") or resource.get("board-name"),
        serial_number=board.get("serial-number"),
        version=resource.get("version"),
    )


async def _routerboard(connection: RouterOSConnection) -> dict[str, str]:
    """Model and serial; virtual routers (CHR) and some x86 builds have no routerboard."""
    try:
        return (await connection.call("/system/routerboard/print") or [{}])[0]
    except ConnectionLost:
        raise
    except RouterOSError:
        return {}
