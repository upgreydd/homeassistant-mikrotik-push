"""Config flow and device tracker inside Home Assistant."""

import pytest
from fake_router import until
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mikrotik_push.const import DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_SSL, CONF_USERNAME, CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

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


async def start_flow(hass):
    await async_setup_component(hass, "network", {})
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})


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
