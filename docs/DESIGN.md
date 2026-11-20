# Design

## Goal
Presence tracking for MikroTik RouterOS 7 where the router pushes changes to Home Assistant, instead of
Home Assistant polling the router. `iot_class: local_push`, domain `mikrotik_push`, no third-party Python
requirements.

## Why not fix `tomaae/homeassistant-mikrotik_router`
It runs one 2.6k-line coordinator that polls ~25 API paths every 30 s with blocking `librouteros` calls
behind one global lock, and pings every known host every 10 s for presence. Most of its size is optional
features (NAT, mangle, filter, queues, captive portal, GPS, UPS).

## Layers (one job per module)
| Module | Job |
|---|---|
| `wire.py` | RouterOS API word/sentence encoding (pure) |
| `routeros.py` | one TCP/TLS connection, login, commands multiplexed by `.tag`, `listen` streams |
| `registry.py` | merges DHCP leases and bridge hosts into hosts by MAC; departure grace (pure) |
| `watcher.py` | snapshot + streams + periodic resync + keepalive + reconnect; HA-free |
| `router_script.py` | generates the router-side setup script with a one-time password (pure) |
| `config_flow.py`, `__init__.py`, `device_tracker.py` | Home Assistant glue |

## Data flow
1. Connect, log in. Start `listen` on `/ip/dhcp-server/lease` and `/interface/bridge/host`.
2. Only then request a `print` snapshot of both tables (the listen commands are already on the wire, so no
   change can fall between snapshot and stream). Events are queued and applied after the snapshot.
3. Every event carries the item's full row; removal arrives as `.id` + `.dead=true`.
4. Every 5 min a full snapshot is compared with the streamed state; any difference is logged and fixed.
   This keeps a silently stalled stream from freezing presence.
5. A cheap command every 30 s detects a dead link that a quiet stream would never reveal.
6. On link loss all entities become unavailable, then reconnect with exponential backoff (1 s .. 60 s,
   kept across quick failures) and resync.
7. A rejected login stops the loop and starts the Home Assistant reauth flow.

## Presence definition
- **Known host:** holds a DHCP lease on the router (enabled). Hosts only seen on the bridge are ignored.
- **Connected:** its MAC is learned on the bridge (`/interface/bridge/host`, dynamic, not `local`, not
  invalid/disabled) **or** it left the bridge less than 180 s ago.
- Arrival is near-instant. Departure takes the bridge ageing time (default 5 min) plus the 180 s grace.
- The grace exists because a port flap or an access-point reboot flushes every MAC learned on that port.
- ARP is not used: it produced ~1.5 events/s (status churn), and a stale entry keeps a device that has left
  looking present, which would make people "home" after they left.
- Idle phones that sleep their Wi-Fi may stop sending frames and age out of the bridge table; they then
  show "not home" after the departure time above until they talk again.

## Entities
One `device_tracker` per known host, `unique_id` = `<config entry id>_<MAC>`. Enabled by default when the lease has a comment or
is static (the admin knows the device); other leases follow Home Assistant's rule (disabled unless another
integration knows the MAC). Hosts remembered from earlier runs reappear as "not home" after a restart.

## Router preparation
The config flow shows a copy/paste script generated per setup: group with only `read,api`, a user limited
to the Home Assistant address, a one-time random password, a self-signed certificate and `api-ssl`.
The script is idempotent (rerun = rotate the password). The host name is validated before it is put into
the script.

## Verified on a real router (RB5009, RouterOS 7.25.1)
- `listen` works on lease, ARP, interface, bridge host, neighbor with a `read,api` user.
- Events carry full rows; removal is `.dead=true` (not `yes`); no initial dump on subscribe.
- A bridge host appears first with `external=false`, then again with `external=true`.
- Lease events also fire on renewals (`last-seen`, `expires-after`); the registry ignores them.
- `!empty` precedes `!done` for empty results.
- api-ssl fails the TLS handshake while no certificate is assigned.

## Known limitations
- Hosts without a DHCP lease (static IP, other VLAN relays) are not tracked.
- Hosts behind a MAC-rewriting repeater, or on routed ports, are never "connected" (not on the bridge).
- MAC randomisation creates a new tracker per random MAC; there is no cleanup of old ones yet.
- Certificate verification only works with a certificate the system trusts; the generated self-signed
  certificate needs "verify" off (encrypted, but the router is not authenticated). Pinning is planned.
- The 180 s grace and 5 min resync are constants, not options.
- Not yet measured: events at AP reboot / port flap and sleeping phones over a long period.
- Departure is slow by design (about 5-8 min): the bridge ageing time plus the grace.
- The router user needs read access to both tables; otherwise setup reports `router_error`.

## Roadmap
1. Long capture on the real router (AP reboot, sleeping phones) and tune the grace.
2. Options flow (grace, which VLANs/bridges to track), certificate pinning, diagnostics, cleanup of old
   randomised-MAC trackers, minimum RouterOS version check, Wi-Fi registration table as a faster signal.
3. Interface/WAN state and system sensors, `update` entity; netwatch webhook push.
4. Release tag equal to the manifest version, submit to `hacs/default`.
