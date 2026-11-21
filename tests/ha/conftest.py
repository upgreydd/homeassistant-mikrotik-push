"""Fixtures for tests that run inside a real Home Assistant (pytest-homeassistant-custom-component)."""

import pathlib
import sys

TESTS_DIR = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR.parent))  # makes `custom_components` importable

import pytest
from fake_router import (
    FakeRouter,
    certificate_sha256,
    make_self_signed_certificate,
    server_ssl_context,
)


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/ from the repository."""


def populate(fake: FakeRouter) -> None:
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
            "/ip/route": [
                {".id": "*1", "dst-address": "0.0.0.0/0", "routing-table": "main", "gateway": "10.0.0.1",
                 "immediate-gw": "10.0.0.1%ether1", "distance": "1", "active": "true", "comment": "FIB DHCP"},
                {".id": "*2", "dst-address": "0.0.0.0/0", "routing-table": "main", "gateway": "10.1.0.1",
                 "immediate-gw": "10.1.0.1%ether2", "distance": "10"},
                {".id": "*3", "dst-address": "0.0.0.0/0", "routing-table": "VPN", "gateway": "wg0",
                 "immediate-gw": "wg0", "distance": "1", "active": "true"},
                {".id": "*4", "dst-address": "10.9.0.0/24", "routing-table": "main", "gateway": "bridge",
                 "immediate-gw": "bridge", "distance": "0", "active": "true"},
            ],
            "/interface": [
                {".id": "*1", "name": "ether1", "type": "ether", "running": "true"},
                {".id": "*2", "name": "ether2", "type": "ether", "running": "false"},
            ],
        }
    )
    fake.ethernet_monitor["ether1"] = {"name": "ether1", "status": "link-ok", "rate": "1Gbps"}


@pytest.fixture
async def router(socket_enabled):
    fake = FakeRouter()
    populate(fake)
    fake.port = await fake.start()
    yield fake
    fake.stop()


@pytest.fixture
async def tls_router(socket_enabled, tmp_path):
    """Like `router`, but over TLS with a self-signed certificate (what api-ssl presents)."""
    certificate, key = make_self_signed_certificate(tmp_path)
    fake = FakeRouter()
    populate(fake)
    fake.certificate_sha256 = certificate_sha256(certificate)
    fake.port = await fake.start(server_ssl_context(certificate, key))
    yield fake
    fake.stop()
