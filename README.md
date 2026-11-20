# MikroTik Push for Home Assistant

Presence tracking for MikroTik RouterOS 7 that listens to the router instead of polling it: devices appear
the moment the router learns them. One `device_tracker` per DHCP-leased host. Arrival is near-instant; departure takes about 5-8 minutes
(bridge ageing plus a grace period). Trackers of leases without a comment that are not static start
disabled, as Home Assistant does for unknown devices; enable the ones you want.

- Router preparation (generated script, permissions): [docs/ROUTER_SETUP.md](docs/ROUTER_SETUP.md)
- Design, presence definition and limitations: [docs/DESIGN.md](docs/DESIGN.md)

## Install (HACS custom repository)
HACS -> Integrations -> three dots -> Custom repositories -> add this repository as *Integration*, install,
restart Home Assistant, then Settings -> Devices & services -> Add integration -> *MikroTik Push*.

## Develop
```
python -m pytest                 # protocol, registry and watcher tests (no Home Assistant needed)
pip install pytest-homeassistant-custom-component
python -m pytest tests/ha        # config flow and entities inside a real Home Assistant
```
