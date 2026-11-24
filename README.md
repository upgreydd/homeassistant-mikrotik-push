# MikroTik Push for Home Assistant

Presence tracking for MikroTik RouterOS 7 that listens to the router instead of polling it: devices appear
the moment the router learns them. One `device_tracker` per DHCP-leased host. Arrival is near-instant; departure takes about 5-8 minutes
(bridge ageing plus a grace period). Trackers of leases without a comment that are not static start
disabled, as Home Assistant does for unknown devices; enable the ones you want.

- Router preparation (generated script, permissions): [docs/ROUTER_SETUP.md](docs/ROUTER_SETUP.md)
- Design, presence definition and limitations: [docs/DESIGN.md](docs/DESIGN.md)

## What you get
- **Presence** (always on): one `device_tracker` per DHCP-leased host.
- **Router device** (always): manufacturer, model, RouterOS version and serial number.
- Optional, off until you select it (Settings -> Devices & services -> MikroTik Push -> Configure):
  - **Ports**: link state (`binary_sensor`) and negotiated speed (`sensor`) of every ethernet port.
  - **Traffic**: receive and transmit rate per port.
  - **System**: CPU load, memory usage, last boot, temperatures and voltages, and an `update` entity
    showing the installed and the latest RouterOS version the router knows of (it is read-only: no install).
  - **Uplink**: one `sensor`, *Active uplink*, holding the interface the router's active default route
    (IPv4, main routing table) leaves through: the route the router itself uses for traffic without a routing
    mark. Its attributes `gateway`, `distance` and `comment` describe that route; with several active routes
    of the same table (load sharing) the lowest distance wins and an ECMP route lists its interfaces
    separated by commas. Policy routing (VPN or per-client tables) is not modelled: those tables appear
    in the list, not in the state.
    The attribute `routes` lists **every enabled** default route (main table first, then each table by
    distance; each with `interface`, `gateway`, `distance`, `table`, `active`, `comment`), so a template
    can show any of them. `interface` is empty while the router cannot say (a down link of a static gateway).
    Pick by a stable key rather than a position, since the list shrinks when a route is withdrawn:
    `{{ state_attr('sensor.router_active_uplink', 'routes') | selectattr('comment', 'eq', 'LTE DHCP') | map(attribute='active') | first | default(false) }}`.
    *Unknown* means no active default route in the main table, *unavailable* that the router could not be
    read. Read every *update interval* (a failover shows up within that time); needs only `read` and `api`.
    IPv6 is not covered.

The router pushes all of this over the one connection Home Assistant keeps open (see
[docs/DESIGN.md](docs/DESIGN.md)). No state is polled in a cycle except temperatures (once a minute), the active default route (every update
interval) and the update status (every 6 hours). Two safety checks run besides: a 30 s keepalive and a full re-read of
the lease and host tables (and of the port list while ports or traffic are on) every 5 minutes,
which repair a stream that silently stopped. Measured on
an RB5009 with every port streaming: no change in router CPU, about 13 kbit/s of traffic.

### History in the database
Traffic and system values change every few seconds. They have no `state_class` (so no long-term
statistics) and Home Assistant writes them at most once per *update interval* (default 30 s), as the
mean of the 5 s samples in that window. Home Assistant offers no per-entity recorder switch to an
integration, so the history is written like any other sensor once you select a feature. With all ports,
traffic and system selected on a router with 9 ports that is about 20 rows per 30 s, roughly 58,000
rows a day (a longer update interval divides that). To keep them out of the history entirely, add this
to `configuration.yaml`, replacing `router` with the slug of your router's device name (for a router
named `RB5009UG` it is `rb5009ug`), so that sensors of other integrations are not excluded:

```yaml
recorder:
  exclude:
    entity_globs:
      - sensor.router_*_rx
      - sensor.router_*_tx
      - sensor.router_cpu_load
      - sensor.router_memory_usage
```

If the router gets another address or API port, use *Reconfigure* on the integration entry (the router must
be the same one); entities, names and dashboards stay.

## Install
In HACS search for *MikroTik Push* (or
[open it in HACS directly](https://my.home-assistant.io/redirect/hacs_repository/?owner=upgreydd&repository=homeassistant-mikrotik-push&category=integration)),
download it, restart Home Assistant, then Settings -> Devices & services -> Add integration -> *MikroTik Push*.

If it does not show up in the search (an older HACS), add it as a custom repository instead:
HACS -> three dots -> Custom repositories -> add this repository as *Integration*, then continue as above.

## Develop
```
python -m pytest                 # protocol, registry and watcher tests (no Home Assistant needed)
pip install pytest-homeassistant-custom-component
python -m pytest tests/ha        # config flow and entities inside a real Home Assistant
```
