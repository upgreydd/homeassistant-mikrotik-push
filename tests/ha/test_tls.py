"""TLS inside Home Assistant: which context is used, and pinning on first use."""

import asyncio
import ssl

from custom_components.mikrotik_push.connection import open_connection
from custom_components.mikrotik_push.const import CONF_CERT_FINGERPRINT, DOMAIN
from custom_components.mikrotik_push.routeros import CertificateChanged
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_SSL, CONF_USERNAME, CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from fake_router import until
from ha_helpers import start_flow


def tls_data(router, **extra):
    return {CONF_HOST: "127.0.0.1", CONF_PORT: router.port, CONF_USERNAME: "user",
            CONF_PASSWORD: "secret", CONF_SSL: True, CONF_VERIFY_SSL: False, **extra}


async def test_unverified_tls_connects_to_a_self_signed_router(hass: HomeAssistant, tls_router):
    connection = await open_connection(tls_data(tls_router))

    assert await connection.call("/system/identity/print") == [{"name": "fake"}]
    await connection.close()


async def test_verified_tls_refuses_a_self_signed_certificate(hass: HomeAssistant, tls_router):
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler() or (lambda _, context: loop.default_exception_handler(context))

    def ignore_the_aborted_handshake(loop, context):
        # The fake server reports the client hanging up in the middle of the handshake, at a time
        # the test cannot predict; the loop is this test's own, so the handler is left in place.
        if not isinstance(context.get("exception"), ConnectionResetError):
            previous(loop, context)

    loop.set_exception_handler(ignore_the_aborted_handshake)

    with pytest.raises(ssl.SSLError):
        await open_connection(tls_data(tls_router, **{CONF_VERIFY_SSL: True}))


async def test_a_pinned_fingerprint_that_no_longer_matches_is_refused_before_login(
    hass: HomeAssistant, tls_router
):
    with pytest.raises(CertificateChanged):
        await open_connection(tls_data(tls_router, **{CONF_CERT_FINGERPRINT: "0" * 64}))

    assert tls_router.login_count == 0


async def test_the_flow_pins_the_certificate_the_router_presents(hass: HomeAssistant, tls_router):
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1"}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {k: v for k, v in tls_data(tls_router).items() if k != CONF_HOST}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CERT_FINGERPRINT] == tls_router.certificate_sha256
    await hass.config_entries.async_unload(result["result"].entry_id)


async def test_a_changed_certificate_stops_setup_and_asks_the_user(hass: HomeAssistant, tls_router):
    entry = MockConfigEntry(
        domain=DOMAIN, data=tls_data(tls_router, **{CONF_CERT_FINGERPRINT: "0" * 64}),
        unique_id="SN123",
    )
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert tls_router.login_count == 0
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


async def test_a_changed_certificate_is_shown_first_and_trusted_only_after_a_second_submit(
    hass: HomeAssistant, tls_router
):
    entry = MockConfigEntry(
        domain=DOMAIN, data=tls_data(tls_router, **{CONF_CERT_FINGERPRINT: "0" * 64}),
        unique_id="SN123",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    flow = next(iter(hass.config_entries.flow.async_progress()))

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_PASSWORD: "new-password"}
    )

    assert result["errors"] == {"base": "certificate_changed"}
    assert result["description_placeholders"]["fingerprint"] == tls_router.certificate_sha256
    assert tls_router.login_count == 0  # the password was not sent to the unknown certificate
    assert entry.data[CONF_CERT_FINGERPRINT] == "0" * 64

    tls_router.password = "new-password"
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_PASSWORD: "new-password"}
    )

    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_CERT_FINGERPRINT] == tls_router.certificate_sha256
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)


async def test_an_entry_from_before_pinning_pins_the_certificate_at_its_next_start(
    hass: HomeAssistant, tls_router
):
    entry = MockConfigEntry(domain=DOMAIN, data=tls_data(tls_router), unique_id="SN123")
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)

    assert entry.data[CONF_CERT_FINGERPRINT] == tls_router.certificate_sha256
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_reconnect_is_checked_against_the_pin_stored_since_the_start(
    hass: HomeAssistant, tls_router
):
    entry = MockConfigEntry(domain=DOMAIN, data=tls_data(tls_router), unique_id="SN123")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    # The router "starts presenting another certificate": the stored pin no longer matches.
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_CERT_FINGERPRINT: "0" * 64}
    )
    tls_router.drop_connections()

    await until(
        lambda: any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())
    )
    await hass.config_entries.async_unload(entry.entry_id)
