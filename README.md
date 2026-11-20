# MikroTik Push for Home Assistant

RouterOS 7 integration that listens to the router instead of polling it. Work in progress: milestone 0
(transport, config flow, HACS packaging). No entities yet.

- Design: [docs/DESIGN.md](docs/DESIGN.md)
- Router preparation (user, permissions, API service): [docs/ROUTER_SETUP.md](docs/ROUTER_SETUP.md)

## Install (HACS custom repository)
HACS -> Integrations -> three dots -> Custom repositories -> add this repo as *Integration*, install,
restart Home Assistant, then Settings -> Devices & services -> Add integration -> *MikroTik Push*.

## Develop
```
python -m pytest        # protocol tests against a fake RouterOS server
```
