"""Config flow and device tracker inside Home Assistant."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from fake_router import until
from ha_helpers import start_flow
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.mikrotik_push.const import DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_SSL, CONF_USERNAME, CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

PHONE_MAC = "AA:BB:CC:DD:EE:01"
PHONE = "device_tracker.phone"


def entry_data(router, password="secret"):
    return {CONF_HOST: "127.0.0.1", CONF_PORT: router.port, CONF_USERNAME: "user",
            CONF_PASSWORD: password, CONF_SSL: False, CONF_VERIFY_SSL: False}


def field_default(form, name):
    key = next(k for k in form["data_schema"].schema if k == name)
    return key.default()


def password_default(form):
    return field_default(form, CONF_PASSWORD)


async def test_flow_shows_a_setup_script_with_the_prefilled_one_time_password(
    hass: HomeAssistant, router
):
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1"}
    )

    assert result["step_id"] == "credentials"
    password = password_default(result)
    script = field_default(result, "setup_script")
    assert f'password="{password}"' in script
    assert 'name="mikrotik-push"' in script


async def test_flow_creates_the_entry_when_the_login_works(hass: HomeAssistant, router):
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1"}
    )
    user_input = {k: v for k, v in entry_data(router).items() if k != CONF_HOST}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "fake"
    assert result["result"].unique_id == "SN123"
    assert "setup_script" not in result["data"]
    await hass.config_entries.async_unload(result["result"].entry_id)


async def test_flow_reports_a_rejected_password(hass: HomeAssistant, router):
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1"}
    )
    user_input = {k: v for k, v in entry_data(router, "wrong").items() if k != CONF_HOST}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    assert result["errors"] == {"base": "invalid_auth"}


async def test_flow_refuses_a_host_that_could_break_out_of_the_script(hass: HomeAssistant):
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: 'x"; /system reset-configuration; "'}
    )
    assert result["errors"] == {CONF_HOST: "invalid_host"}


async def test_the_same_router_cannot_be_added_twice(hass: HomeAssistant, router):
    MockConfigEntry(domain=DOMAIN, data=entry_data(router), unique_id="SN123").add_to_hass(hass)
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1"}
    )
    user_input = {k: v for k, v in entry_data(router).items() if k != CONF_HOST}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.fixture
async def loaded_entry(hass: HomeAssistant, router):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(router), unique_id="SN123")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_leased_host_on_the_bridge_is_home(hass: HomeAssistant, loaded_entry):
    await until(lambda: hass.states.get(PHONE) and hass.states.get(PHONE).state == "home")
    state = hass.states.get(PHONE)
    assert state.attributes["ip"] == "10.0.0.5"
    assert state.attributes["mac"] == PHONE_MAC
    assert state.attributes["port"] == "ether3"


async def test_new_named_lease_creates_an_enabled_tracker_with_the_lease_name(
    hass: HomeAssistant, router, loaded_entry
):
    await until(lambda: hass.states.get(PHONE))

    await router.push(
        "/ip/dhcp-server/lease",
        {".id": "*2", "mac-address": "AA:BB:CC:DD:EE:02", "address": "10.0.0.6",
         "status": "bound", "host-name": "laptop", "comment": "Laptop"},
    )

    await until(lambda: hass.states.get("device_tracker.laptop"))
    assert hass.states.get("device_tracker.laptop").state == "not_home"


async def test_unnamed_dynamic_lease_is_registered_but_disabled(
    hass: HomeAssistant, router, loaded_entry
):
    await until(lambda: hass.states.get(PHONE))

    await router.push(
        "/ip/dhcp-server/lease",
        {".id": "*3", "mac-address": "AA:BB:CC:DD:EE:03", "address": "10.0.0.7",
         "status": "bound", "host-name": "stranger", "dynamic": "true"},
    )

    registry = er.async_get(hass)
    await until(lambda: registry.async_get("device_tracker.stranger"))
    assert registry.async_get("device_tracker.stranger").disabled_by is not None
    assert hass.states.get("device_tracker.stranger") is None


async def test_known_hosts_come_back_as_not_home_after_a_restart(hass: HomeAssistant, router):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(router), unique_id="SN123")
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "device_tracker", DOMAIN, f"{entry.entry_id}_AA:BB:CC:DD:EE:09",
        config_entry=entry, original_name="Gone phone", suggested_object_id="gone_phone",
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # the host holds no lease any more, yet it shows "not home" instead of "unavailable"
    await until(lambda: hass.states.get("device_tracker.gone_phone") and
                hass.states.get("device_tracker.gone_phone").state == "not_home")
    await hass.config_entries.async_unload(entry.entry_id)


async def test_trackers_become_unavailable_when_the_router_link_drops(
    hass: HomeAssistant, router, loaded_entry
):
    await until(lambda: hass.states.get(PHONE) and hass.states.get(PHONE).state == "home")

    router.drop_connections()
    await until(lambda: hass.states.get(PHONE).state == "unavailable")
    await until(lambda: hass.states.get(PHONE).state == "home", timeout=6)


async def test_rejected_login_at_setup_starts_reauth(hass: HomeAssistant, router):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(router, "wrong"), unique_id="SN123")
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress())


async def test_reauth_accepts_the_new_password_and_reloads(hass: HomeAssistant, router):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(router, "wrong"), unique_id="SN123")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    flow = next(iter(hass.config_entries.flow.async_progress()))

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_PASSWORD: "secret"}
    )

    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "secret"
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_flow_hands_the_source_address_helper_an_ip_not_a_name(hass: HomeAssistant, router):
    with patch(
        "custom_components.mikrotik_push.config_flow.async_get_source_ip", return_value="10.0.0.2"
    ) as source_ip:
        result = await start_flow(hass)
        await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "localhost"})

    assert source_ip.call_args.kwargs["target_ip"] == "127.0.0.1"


async def test_reconfigure_moves_an_entry_that_cannot_reach_its_router(hass: HomeAssistant, router):
    entry = MockConfigEntry(
        domain=DOMAIN, data={**entry_data(router), CONF_PORT: 1}, unique_id="SN123"
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY

    form = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        form["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: router.port}
    )

    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PORT] == router.port
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    await hass.config_entries.async_unload(entry.entry_id)


async def test_reconfigure_refuses_another_router(hass: HomeAssistant, router):
    router.tables["/system/routerboard"] = [{"serial-number": "OTHER"}]
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(router), unique_id="SN123")
    entry.add_to_hass(hass)

    form = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        form["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: router.port}
    )

    assert result["reason"] == "unique_id_mismatch"


async def test_reconfigure_refuses_a_host_that_could_break_out_of_a_script(
    hass: HomeAssistant, router
):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(router), unique_id="SN123")
    entry.add_to_hass(hass)

    form = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        form["flow_id"], {CONF_HOST: 'x"; /system reset', CONF_PORT: router.port}
    )

    assert result["errors"] == {CONF_HOST: "invalid_host"}


async def test_reconfigure_moves_a_router_without_a_serial_number_known_by_its_address(
    hass: HomeAssistant, router
):
    del router.tables["/system/routerboard"]  # like a CHR: the address is its identity
    entry = MockConfigEntry(
        domain=DOMAIN, data={**entry_data(router), CONF_HOST: "10.9.9.9"}, unique_id="10.9.9.9"
    )
    entry.add_to_hass(hass)

    form = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        form["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: router.port}
    )

    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == "127.0.0.1" and entry.data[CONF_HOST] == "127.0.0.1"
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)


async def test_flow_reports_unreadable_tables_as_a_router_error(hass: HomeAssistant, router):
    del router.tables["/interface/bridge/host"]  # the account may not read it
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1"}
    )
    user_input = {k: v for k, v in entry_data(router).items() if k != CONF_HOST}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    assert result["errors"] == {"base": "router_error"}


async def test_tracker_unique_ids_are_scoped_to_the_config_entry(
    hass: HomeAssistant, loaded_entry
):
    await until(lambda: hass.states.get(PHONE))
    registry = er.async_get(hass)
    assert registry.async_get(PHONE).unique_id == f"{loaded_entry.entry_id}_{PHONE_MAC}"


async def test_static_lease_without_a_comment_is_enabled_by_default(
    hass: HomeAssistant, router, loaded_entry
):
    await until(lambda: hass.states.get(PHONE))

    await router.push(
        "/ip/dhcp-server/lease",
        {".id": "*4", "mac-address": "AA:BB:CC:DD:EE:04", "address": "10.0.0.8",
         "status": "bound", "host-name": "printer", "dynamic": "false"},
    )

    await until(lambda: hass.states.get("device_tracker.printer"))


async def test_login_rejected_while_running_starts_reauth(
    hass: HomeAssistant, router, loaded_entry
):
    await until(lambda: hass.states.get(PHONE) and hass.states.get(PHONE).state == "home")

    router.password = "changed-on-the-router"
    router.drop_connections()

    await until(
        lambda: any(
            flow["context"]["source"] == "reauth"
            for flow in hass.config_entries.flow.async_progress()
        ),
        timeout=6,
    )


async def test_reauth_shows_a_fresh_script_for_the_new_password(hass: HomeAssistant, router):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(router, "wrong"), unique_id="SN123")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    flow = next(iter(hass.config_entries.flow.async_progress()))

    form = await hass.config_entries.flow.async_configure(flow["flow_id"])

    assert form["step_id"] == "reauth_confirm"
    assert f'password="{password_default(form)}"' in field_default(form, "setup_script")


async def test_reauth_refuses_a_different_router(hass: HomeAssistant, router):
    router.tables["/system/routerboard"] = [{"serial-number": "OTHER"}]
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(router, "wrong"), unique_id="SN123")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    flow = next(iter(hass.config_entries.flow.async_progress()))

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_PASSWORD: "secret"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"


async def test_unloading_stops_the_watcher_and_closes_the_connection(
    hass: HomeAssistant, router, loaded_entry
):
    await until(lambda: hass.states.get(PHONE))
    watcher = loaded_entry.runtime_data.watcher

    assert await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()

    assert not watcher.connected
    await until(lambda: all(writer.is_closing() for writer in router._writers))


ALL_FEATURES = {"track_ports": True, "track_traffic": True, "track_system": True, "update_interval": 5}


def state_of(hass, entry, domain, tail):
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}_{tail}")
    return hass.states.get(entity_id) if entity_id else None


@pytest.fixture
async def full_entry(hass: HomeAssistant, router):
    entry = MockConfigEntry(
        domain=DOMAIN, data=entry_data(router), options=ALL_FEATURES, unique_id="SN123"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_router_is_a_device_with_model_and_version(hass: HomeAssistant, loaded_entry):
    (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), loaded_entry.entry_id)

    assert (device.manufacturer, device.model, device.sw_version) == ("MikroTik", "RB-TEST", "7.25.1")
    assert device.serial_number == "SN123"


async def test_nothing_optional_exists_until_it_is_selected(hass: HomeAssistant, loaded_entry):
    await until(lambda: hass.states.get(PHONE))
    assert not hass.states.async_entity_ids("binary_sensor")
    assert not hass.states.async_entity_ids("sensor")
    assert not hass.states.async_entity_ids("update")


async def test_ports_report_link_and_speed(hass: HomeAssistant, full_entry):
    await until(lambda: state_of(hass, full_entry, "binary_sensor", "port_ether1_link"))

    assert state_of(hass, full_entry, "binary_sensor", "port_ether1_link").state == "on"
    assert state_of(hass, full_entry, "binary_sensor", "port_ether2_link").state == "off"
    assert state_of(hass, full_entry, "sensor", "port_ether1_speed").state == "1000"
    assert state_of(hass, full_entry, "sensor", "port_ether2_speed").state == "unknown"


async def test_a_link_change_is_pushed_into_the_port_entities(
    hass: HomeAssistant, router, full_entry
):
    await until(lambda: state_of(hass, full_entry, "binary_sensor", "port_ether2_link"))
    router.ethernet_monitor["ether2"] = {"name": "ether2", "status": "link-ok", "rate": "2.5Gbps"}

    await router.push(
        "/interface",
        {".id": "*2", "name": "ether2", "type": "ether", "running": "true"},
    )

    await until(lambda: state_of(hass, full_entry, "binary_sensor", "port_ether2_link").state == "on")
    await until(lambda: state_of(hass, full_entry, "sensor", "port_ether2_speed").state == "2500")


async def test_traffic_load_and_update_arrive_as_entities(hass: HomeAssistant, router, full_entry):
    await until(lambda: state_of(hass, full_entry, "update", "system_update"))
    await router.wait_for_subscribers(5)  # leases, bridge, interfaces, traffic, resource

    await router.push(
        "/interface/monitor-traffic",
        {"name": "ether1", "rx-bits-per-second": "8000000", "tx-bits-per-second": "1000000"},
    )
    await router.push("/system/resource/monitor", {"cpu-used": "7", "free-memory": "512000"})

    await until(lambda: state_of(hass, full_entry, "sensor", "port_ether1_rx"))
    assert float(state_of(hass, full_entry, "sensor", "port_ether1_rx").state) == 8.0  # Mbit/s
    assert float(state_of(hass, full_entry, "sensor", "port_ether1_tx").state) == 1.0
    await until(lambda: state_of(hass, full_entry, "sensor", "system_cpu").state == "7")
    assert float(state_of(hass, full_entry, "sensor", "system_memory").state) == 50.0
    assert state_of(hass, full_entry, "sensor", "system_boot").state not in ("unknown", "unavailable")
    assert float(state_of(hass, full_entry, "sensor", "system_health_cpu-temperature").state) == 51.0
    update = state_of(hass, full_entry, "update", "system_update")
    assert (update.state, update.attributes["installed_version"], update.attributes["latest_version"]) == (
        "on", "7.25.1", "7.26",
    )


async def test_the_options_flow_turns_features_on_and_reloads(
    hass: HomeAssistant, router, loaded_entry
):
    await until(lambda: hass.states.get(PHONE))

    form = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    assert form["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(form["flow_id"], ALL_FEATURES)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    await until(lambda: state_of(hass, loaded_entry, "binary_sensor", "port_ether1_link"))


async def test_unselecting_a_feature_removes_its_entities(hass: HomeAssistant, full_entry):
    await until(lambda: state_of(hass, full_entry, "sensor", "port_ether1_speed"))

    form = await hass.config_entries.options.async_init(full_entry.entry_id)
    await hass.config_entries.options.async_configure(
        form["flow_id"], {**ALL_FEATURES, "track_traffic": False, "track_system": False}
    )
    await hass.async_block_till_done()

    await until(lambda: state_of(hass, full_entry, "binary_sensor", "port_ether1_link"))
    assert state_of(hass, full_entry, "sensor", "port_ether1_rx") is None
    assert state_of(hass, full_entry, "sensor", "system_cpu") is None
    assert state_of(hass, full_entry, "update", "system_update") is None
    assert state_of(hass, full_entry, "sensor", "port_ether1_speed") is not None


async def test_a_router_without_health_readings_keeps_presence_working(
    hass: HomeAssistant, router
):
    del router.tables["/system/health"]
    entry = MockConfigEntry(
        domain=DOMAIN, data=entry_data(router), options=ALL_FEATURES, unique_id="SN123"
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await until(lambda: hass.states.get(PHONE) and hass.states.get(PHONE).state == "home")
    await router.wait_for_subscribers(5)
    await router.push("/system/resource/monitor", {"cpu-used": "3", "free-memory": "1024"})
    await until(lambda: state_of(hass, entry, "sensor", "system_cpu").state == "3")
    assert hass.states.get(PHONE).state == "home"
    assert router.login_count == 1  # the session was never torn down
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_update_entity_is_unknown_until_the_router_has_checked(
    hass: HomeAssistant, router
):
    router.tables["/system/package/update"] = [
        {"installed-version": "7.25.1", "latest-version": "", "status": "ERROR"}
    ]
    entry = MockConfigEntry(
        domain=DOMAIN, data=entry_data(router), options=ALL_FEATURES, unique_id="SN123"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await until(lambda: state_of(hass, entry, "update", "system_update"))
    await until(lambda: state_of(hass, entry, "update", "system_update").state != "unavailable")

    assert state_of(hass, entry, "update", "system_update").state == "unknown"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_switching_a_feature_off_and_on_keeps_what_the_user_did_to_its_entities(
    hass: HomeAssistant, full_entry
):
    await until(lambda: state_of(hass, full_entry, "sensor", "port_ether1_speed"))
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{full_entry.entry_id}_port_ether1_speed"
    )
    registry.async_update_entity(entity_id, new_entity_id="sensor.wan_speed", name="WAN speed")

    form = await hass.config_entries.options.async_init(full_entry.entry_id)
    await hass.config_entries.options.async_configure(
        form["flow_id"], {**ALL_FEATURES, "track_ports": False}
    )
    await hass.async_block_till_done()

    assert registry.async_get("sensor.wan_speed").disabled_by is er.RegistryEntryDisabler.CONFIG_ENTRY
    assert hass.states.get("sensor.wan_speed") is None

    form = await hass.config_entries.options.async_init(full_entry.entry_id)
    with patch.object(
        hass.config_entries, "async_schedule_reload", wraps=hass.config_entries.async_schedule_reload
    ) as reload:
        await hass.config_entries.options.async_configure(form["flow_id"], ALL_FEATURES)
        await hass.async_block_till_done()
        await until(lambda: hass.states.get("sensor.wan_speed"))
        reloads_for_the_options_change = reload.call_count

        # Enabling an entity from the registry makes Home Assistant reload the entry 30 s later,
        # unless it was disabled by the config entry itself.
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=45))
        await hass.async_block_till_done()

        assert reload.call_count == reloads_for_the_options_change
    assert registry.async_get("sensor.wan_speed").name == "WAN speed"
    assert registry.async_get("sensor.wan_speed").disabled_by is None


async def test_setup_is_retried_when_the_router_stops_answering(
    hass: HomeAssistant, router, monkeypatch
):
    monkeypatch.setattr("custom_components.mikrotik_push.routeros.REQUEST_TIMEOUT", 0.3)
    router.ignored_commands.add("/system/resource/print")
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(router), unique_id="SN123")
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_flow_reports_cannot_connect_when_the_router_stops_answering(
    hass: HomeAssistant, router, monkeypatch
):
    monkeypatch.setattr("custom_components.mikrotik_push.routeros.REQUEST_TIMEOUT", 0.3)
    router.ignored_commands.add("/system/resource/print")
    result = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1"}
    )
    user_input = {k: v for k, v in entry_data(router).items() if k != CONF_HOST}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)

    assert result["errors"] == {"base": "cannot_connect"}


async def test_old_hosts_nobody_named_are_forgotten_once_the_router_lists_none_of_them(
    hass: HomeAssistant, router, monkeypatch
):
    monkeypatch.setattr("custom_components.mikrotik_push.device_tracker.STALE_AFTER", timedelta(0))
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(router), unique_id="SN123")
    entry.add_to_hass(hass)
    registry = er.async_get(hass)

    def register(mac, **options):
        return registry.async_get_or_create(
            "device_tracker", DOMAIN, f"{entry.entry_id}_{mac}", config_entry=entry, **options
        )

    stale = register("AA:BB:CC:00:00:99", disabled_by=er.RegistryEntryDisabler.INTEGRATION)
    chosen = register("AA:BB:CC:00:00:98")  # enabled: somebody cares about this one
    leased = register(PHONE_MAC, disabled_by=er.RegistryEntryDisabler.INTEGRATION)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await until(lambda: registry.async_get(stale.entity_id) is None)
    assert registry.async_get(chosen.entity_id) is not None
    assert registry.async_get(leased.entity_id) is not None  # the router still lists a lease

    await router.push(
        "/ip/dhcp-server/lease",
        {".id": "*9", "mac-address": "AA:BB:CC:00:00:99", "address": "10.0.0.9", "status": "bound",
         "host-name": "returner", "comment": "Returner"},
    )
    # A forgotten host that returns gets a tracker again (Home Assistant restores its old flags).
    await until(
        lambda: registry.async_get_entity_id("device_tracker", DOMAIN, f"{entry.entry_id}_AA:BB:CC:00:00:99")
    )
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_active_uplink_follows_a_failover(hass: HomeAssistant, router):
    entry = MockConfigEntry(
        domain=DOMAIN, data=entry_data(router), options={"track_uplink": True, "update_interval": 5}, unique_id="SN123"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await until(lambda: state_of(hass, entry, "sensor", "uplink").state == "ether1")
    attributes = state_of(hass, entry, "sensor", "uplink").attributes
    assert (attributes["gateway"], attributes["distance"], attributes["comment"]) == ("10.0.0.1", 1, "FIB DHCP")
    assert [r["interface"] for r in attributes["routes"]] == ["ether1", "ether2", "wg0"]

    router.tables["/ip/route"][0]["active"] = "false"  # the failover moves the default route
    router.tables["/ip/route"][1]["active"] = "true"

    await until(lambda: state_of(hass, entry, "sensor", "uplink").state == "ether2", timeout=10)  # polled
    await hass.config_entries.async_unload(entry.entry_id)


async def test_unselecting_the_uplink_removes_its_sensor(hass: HomeAssistant, router):
    entry = MockConfigEntry(
        domain=DOMAIN, data=entry_data(router), options={"track_uplink": True}, unique_id="SN123"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await until(lambda: state_of(hass, entry, "sensor", "uplink"))

    form = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(form["flow_id"], {"track_uplink": False})
    await hass.async_block_till_done()

    await until(lambda: state_of(hass, entry, "sensor", "uplink") is None)

    form = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(form["flow_id"], {"track_uplink": True})
    await hass.async_block_till_done()

    await until(lambda: state_of(hass, entry, "sensor", "uplink").state == "ether1")  # back from disabled
    await hass.config_entries.async_unload(entry.entry_id)
