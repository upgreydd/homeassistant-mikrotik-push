"""Unique ids of the optional entities, and which user-selectable feature each belongs to."""

from __future__ import annotations

_PORT = "port_"
_SYSTEM = "system_"
_UPLINK = "uplink"
_PORT_KINDS_BY_FEATURE = {"link": "ports", "speed": "ports", "rx": "traffic", "tx": "traffic"}


def port_unique_id(entry_id: str, port: str, kind: str) -> str:
    return f"{entry_id}_{_PORT}{port}_{kind}"


def system_unique_id(entry_id: str, kind: str) -> str:
    return f"{entry_id}_{_SYSTEM}{kind}"


def uplink_unique_id(entry_id: str) -> str:
    return f"{entry_id}_{_UPLINK}"


def feature_of(entry_id: str, unique_id: str) -> str | None:
    """`ports`, `traffic`, `system` or `uplink` for an optional entity; None for a device tracker."""
    tail = unique_id.removeprefix(f"{entry_id}_")
    if tail.startswith(_PORT):
        return _PORT_KINDS_BY_FEATURE.get(tail.rpartition("_")[2])
    if tail.startswith(_SYSTEM):
        return "system"
    if tail == _UPLINK:
        return "uplink"
    return None
