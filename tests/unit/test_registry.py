"""Host registry: DHCP leases + bridge hosts merged into hosts by MAC, with departure grace."""

import pytest

from mikrotik_push.registry import HostRegistry, Table

MAC = "AA:BB:CC:DD:EE:01"


def lease(**fields):
    return {".id": "*1", "mac-address": MAC, "address": "10.0.0.5", "status": "bound", **fields}


def bridge_host(**fields):
    return {".id": "*B1", "mac-address": MAC, "on-interface": "ether3", "dynamic": "true",
            "local": "false", **fields}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def registry():
    return HostRegistry()


def test_leased_host_without_bridge_entry_is_known_but_not_connected(registry):
    changed = registry.replace(Table.LEASE, [lease(**{"host-name": "phone"})])
    host = registry.hosts[MAC]
    assert changed == {MAC}
    assert (host.name, host.hostname, host.ip_address, host.connected) == (
        "phone", "phone", "10.0.0.5", False)


def test_lease_comment_is_the_name_and_host_name_stays_the_hostname(registry):
    registry.replace(Table.LEASE, [lease(**{"host-name": "phone", "comment": "Anna's phone"})])
    assert (registry.hosts[MAC].name, registry.hosts[MAC].hostname) == ("Anna's phone", "phone")


@pytest.mark.parametrize(
    ("fields", "administered"),
    [({}, False), ({"comment": "Anna's phone"}, True), ({"dynamic": "false"}, True),
     ({"dynamic": "true"}, False)],
)
def test_host_is_administered_when_named_or_reserved_by_the_admin(registry, fields, administered):
    registry.replace(Table.LEASE, [lease(**fields)])
    assert registry.hosts[MAC].administered is administered


def test_bridge_entry_connects_the_host_and_reports_the_port(registry):
    registry.replace(Table.LEASE, [lease()])
    registry.replace(Table.BRIDGE_HOST, [bridge_host()])
    assert registry.hosts[MAC].connected
    assert registry.hosts[MAC].port == "ether3"


def test_mac_seen_only_on_the_bridge_is_not_a_known_host(registry):
    registry.replace(Table.BRIDGE_HOST, [bridge_host()])
    assert registry.hosts == {}


@pytest.mark.parametrize(
    "ignored",
    [{"local": "true"}, {"invalid": "true"}, {"disabled": "true"}, {"dynamic": "false"}],
)
def test_router_own_invalid_disabled_and_static_bridge_entries_do_not_count(registry, ignored):
    registry.replace(Table.LEASE, [lease()])
    registry.replace(Table.BRIDGE_HOST, [bridge_host(**ignored)])
    assert not registry.hosts[MAC].connected


def test_dead_event_removes_the_bridge_entry(registry):
    registry.replace(Table.LEASE, [lease()])
    registry.replace(Table.BRIDGE_HOST, [bridge_host()])
    assert registry.apply(Table.BRIDGE_HOST, {".id": "*B1", ".dead": "yes"}) == {MAC}
    assert not registry.hosts[MAC].connected


def test_dead_flag_is_accepted_as_true_too(registry):
    registry.replace(Table.LEASE, [lease()])
    registry.replace(Table.BRIDGE_HOST, [bridge_host()])
    registry.apply(Table.BRIDGE_HOST, {".id": "*B1", ".dead": "true"})
    assert not registry.hosts[MAC].connected


def test_event_replaces_the_row_so_removed_attributes_do_not_linger(registry):
    registry.replace(Table.LEASE, [lease(**{"comment": "Anna's phone", "host-name": "phone"})])
    registry.apply(Table.LEASE, lease(**{"host-name": "phone"}))
    assert registry.hosts[MAC].name == "phone"


def test_unchanged_event_reports_no_change(registry):
    registry.replace(Table.LEASE, [lease()])
    assert registry.apply(Table.LEASE, lease()) == set()


def test_bound_lease_wins_over_waiting_lease_for_the_same_mac(registry):
    waiting = lease(**{".id": "*0", "status": "waiting", "address": "10.9.9.9"})
    registry.replace(Table.LEASE, [lease(), waiting])
    assert registry.hosts[MAC].ip_address == "10.0.0.5"


def test_active_address_beats_the_configured_one(registry):
    registry.replace(Table.LEASE, [lease(**{"active-address": "10.0.0.77"})])
    assert registry.hosts[MAC].ip_address == "10.0.0.77"


def test_disabled_lease_is_ignored(registry):
    registry.replace(Table.LEASE, [lease(disabled="true")])
    assert registry.hosts == {}


def test_mac_addresses_are_normalised_to_upper_case(registry):
    registry.replace(Table.LEASE, [lease(**{"mac-address": MAC.lower()})])
    registry.replace(Table.BRIDGE_HOST, [bridge_host(**{"mac-address": MAC.lower()})])
    assert registry.hosts[MAC].connected


def test_port_choice_for_a_mac_on_several_vlans_follows_the_numeric_row_id(registry):
    registry.replace(Table.LEASE, [lease()])
    registry.replace(
        Table.BRIDGE_HOST,
        [bridge_host(**{".id": "*1A", "on-interface": "ether9"}),
         bridge_host(**{".id": "*9", "on-interface": "ether2"})],
    )
    assert registry.hosts[MAC].port == "ether2"  # *9 is numerically lower than *1A


class TestDepartureGrace:
    @pytest.fixture
    def clock(self):
        return FakeClock()

    @pytest.fixture
    def registry(self, clock):
        registry = HostRegistry(departure_grace=180, clock=clock)
        registry.replace(Table.LEASE, [lease()])
        registry.replace(Table.BRIDGE_HOST, [bridge_host()])
        return registry

    def test_host_stays_connected_during_the_grace_period(self, registry, clock):
        clock.now += 100
        changed = registry.apply(Table.BRIDGE_HOST, {".id": "*B1", ".dead": "yes"})
        assert changed == {MAC}  # the port is gone, the host is still connected
        assert registry.hosts[MAC].connected
        assert registry.hosts[MAC].port is None
        assert registry.seconds_until_grace_ends() == pytest.approx(180)

    def test_host_leaves_when_the_grace_period_ends(self, registry, clock):
        registry.apply(Table.BRIDGE_HOST, {".id": "*B1", ".dead": "yes"})
        clock.now += 181
        assert registry.refresh() == {MAC}
        assert not registry.hosts[MAC].connected
        assert registry.seconds_until_grace_ends() is None

    def test_coming_back_inside_the_grace_period_cancels_the_departure(self, registry, clock):
        registry.apply(Table.BRIDGE_HOST, {".id": "*B1", ".dead": "yes"})
        clock.now += 60
        registry.apply(Table.BRIDGE_HOST, bridge_host())
        clock.now += 1000
        assert registry.refresh() == set()
        assert registry.hosts[MAC].connected

    def test_a_host_that_was_never_on_the_bridge_gets_no_grace(self, clock):
        registry = HostRegistry(departure_grace=180, clock=clock)
        registry.replace(Table.LEASE, [lease()])
        assert not registry.hosts[MAC].connected


def wifi_client(**fields):
    return {".id": "*W1", "mac-address": MAC.lower(), "interface": "2GHz_hAP", "ssid": "Home",
            "authorized": "true", **fields}


def test_wifi_registration_connects_the_host_and_reports_the_interface(registry):
    registry.replace(Table.LEASE, [lease()])
    registry.replace(Table.WIFI_REGISTRATION, [wifi_client()])
    assert (registry.hosts[MAC].connected, registry.hosts[MAC].port) == (True, "2GHz_hAP")


def test_the_bridge_port_wins_when_a_mac_is_on_both(registry):
    registry.replace(Table.LEASE, [lease()])
    registry.replace(Table.WIFI_REGISTRATION, [wifi_client()])
    registry.replace(Table.BRIDGE_HOST, [bridge_host()])
    assert registry.hosts[MAC].port == "ether3"


def test_a_client_still_in_its_handshake_does_not_count(registry):
    registry.replace(Table.LEASE, [lease()])
    registry.replace(Table.WIFI_REGISTRATION, [wifi_client(authorized="false")])
    assert not registry.hosts[MAC].connected


def test_a_sleeping_phone_stays_connected_while_registered_after_its_bridge_entry_aged_out():
    clock = FakeClock()
    registry = HostRegistry(departure_grace=180, clock=clock)
    registry.replace(Table.LEASE, [lease()])
    registry.replace(Table.BRIDGE_HOST, [bridge_host()])
    registry.replace(Table.WIFI_REGISTRATION, [wifi_client()])

    registry.apply(Table.BRIDGE_HOST, {".id": "*B1", ".dead": "true"})
    clock.now += 600

    assert registry.refresh() == set() and registry.hosts[MAC].connected
    assert registry.seconds_until_grace_ends() is None


def test_leaving_the_wifi_starts_the_grace_period_like_leaving_the_bridge():
    clock = FakeClock()
    registry = HostRegistry(departure_grace=180, clock=clock)
    registry.replace(Table.LEASE, [lease()])
    registry.replace(Table.WIFI_REGISTRATION, [wifi_client()])

    registry.apply(Table.WIFI_REGISTRATION, {".id": "*W1", ".dead": "true"})
    assert registry.hosts[MAC].connected and registry.seconds_until_grace_ends() == 180

    clock.now += 180
    assert registry.refresh() == {MAC} and not registry.hosts[MAC].connected
