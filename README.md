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

The router pushes all of this over the one connection Home Assistant keeps open (see
[docs/DESIGN.md](docs/DESIGN.md)); nothing is polled except temperatures (once a minute) and the update
status (every 6 hours). Measured on an RB5009 with every port streaming: no change in router CPU, about
13 kbit/s of traffic.

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

## Install (HACS custom repository)
HACS -> Integrations -> three dots -> Custom repositories -> add this repository as *Integration*, install,
restart Home Assistant, then Settings -> Devices & services -> Add integration -> *MikroTik Push*.

## Develop
```
python -m pytest                 # protocol, registry and watcher tests (no Home Assistant needed)
pip install pytest-homeassistant-custom-component
python -m pytest tests/ha        # config flow and entities inside a real Home Assistant
```
