# Design

## Goal
Replace the polling `mikrotik_router` integration for RouterOS 7 with one that lets the router push changes.
`iot_class: local_push`, domain `mikrotik_push`, no third-party Python requirements.

## Why not fix upstream
Upstream (`tomaae/homeassistant-mikrotik_router`) runs one 2.6k-line coordinator that polls ~25 API paths
every 30 s with blocking `librouteros` calls behind one global lock, and pings each known host every 10 s
for presence. Most of its size is optional features (NAT, mangle, filter, queues, captive, GPS, UPS).

## Transport: own asyncio client (`routeros.py`)
- One TCP (optionally TLS) connection, plain login (RouterOS 7), commands multiplexed by `.tag`.
- `call(path)` = request/response; `listen(path)` = async generator over `<path>/listen`, removals arrive as
  `.dead=yes`; leaving the generator sends `/cancel =tag=`.
- Connection loss wakes every waiter with `ConnectionLost`; the coordinator reconnects with backoff and
  re-runs `print` to resync (listen only reports changes, not the initial state).
- Tested against a fake server (`tests/`), no router required.

## Data sources (push where RouterOS can push)
| Need | Source | Mode |
|---|---|---|
| Presence | `/ip/dhcp-server/lease`, `/ip/arp`, `/interface/wifi/registration-table` (or `wireless`) `listen` | push |
| Interface link | `/interface` `listen` | push |
| WAN / host up-down | netwatch `up-script`/`down-script` -> `/tool/fetch` -> HA webhook | push (router config) |
| Resources, health, firmware | one batched call every 30-60 s | poll |

RouterOS docs warn `listen` "may not work everywhere"; each path is verified on the real router before use,
with a poll fallback per path.

## Entities (milestone order)
1. `device_tracker` (DHCP + ARP + wifi merged by MAC), no ping loop.
2. `binary_sensor` interface link / WAN; `sensor` CPU, memory, temperature, uptime; `update` RouterOS.
Out of scope until a real automation needs them: NAT, mangle, filter, queues, kid control, captive portal,
accounting, GPS, UPS, switches/buttons.

## Milestones
0. (done) repo, HACS metadata, CI, transport + tests, config flow, router setup docs.
1. Verify on the real router: permissions, `listen` per path, TLS mode. Fix the doc accordingly.
2. Presence tracker on `listen` with reconnect/resync.
3. Link/WAN + system sensors, update entity.
4. Netwatch webhook push, diagnostics, reauth/reconfigure flows, tests with `pytest-homeassistant-custom-component`.
5. Release tag = `manifest.json` version, submit to `hacs/default` (needs the final GitHub URL).

## Known limitations
- No reauth flow yet (login rejection -> `ConfigEntryError`).
- api-ssl needs a certificate on the router (certificate-less anonymous DH is rejected by modern OpenSSL).
- Placeholder brand icon (`brand/icon.png`).
