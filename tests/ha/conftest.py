"""Fixtures for tests that run inside a real Home Assistant (pytest-homeassistant-custom-component)."""

import pathlib
import sys

TESTS_DIR = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR.parent))  # makes `custom_components` importable

import pytest  # noqa: E402
from fake_router import FakeRouter  # noqa: E402


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/ from the repository."""


@pytest.fixture
async def router(socket_enabled):
    fake = FakeRouter()
    fake.tables.update(
        {
            "/ip/dhcp-server/lease": [
                {".id": "*1", "mac-address": "AA:BB:CC:DD:EE:01", "address": "10.0.0.5",
                 "status": "bound", "host-name": "phone", "comment": "Phone"},
            ],
            "/interface/bridge/host": [
                {".id": "*B1", "mac-address": "AA:BB:CC:DD:EE:01", "on-interface": "ether3",
                 "dynamic": "true", "local": "false"},
            ],
            "/system/routerboard": [{"serial-number": "SN123", "model": "RB-TEST"}],
            "/system/resource": [
                {"uptime": "1d", "total-memory": "1048576000", "version": "7.25.1",
                 "board-name": "RB-TEST"}
            ],
            "/system/package/update": [
                {"installed-version": "7.25.1", "latest-version": "7.26", "status": "New"}
            ],
            "/system/health": [{".id": "*1", "name": "cpu-temperature", "value": "51", "type": "C"}],
            "/interface": [
                {".id": "*1", "name": "ether1", "type": "ether", "running": "true"},
                {".id": "*2", "name": "ether2", "type": "ether", "running": "false"},
            ],
        }
    )
    fake.ethernet_monitor["ether1"] = {"name": "ether1", "status": "link-ok", "rate": "1Gbps"}
    fake.port = await fake.start()
    yield fake
    fake.stop()
