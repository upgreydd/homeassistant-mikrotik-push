"""Open a RouterOS connection from a config entry's data."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.util.ssl import client_context, client_context_no_verify

from .routeros import RouterOSConnection


async def open_connection(config: Mapping[str, Any]) -> RouterOSConnection:
    ssl_context = None
    if config[CONF_SSL]:
        ssl_context = client_context() if config[CONF_VERIFY_SSL] else client_context_no_verify()
    return await RouterOSConnection.open(
        config[CONF_HOST],
        config[CONF_PORT],
        config[CONF_USERNAME],
        config[CONF_PASSWORD],
        ssl_context=ssl_context,
    )
