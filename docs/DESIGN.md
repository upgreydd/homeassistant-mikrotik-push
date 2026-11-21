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
| `telemetry.py` | optional readings: port state, traffic, CPU/memory, health, RouterOS update; HA-free |
| `router_info.py` | name, model, serial number and version, read once per connection |
| `router_script.py` | generates the router-side setup script with a one-time password (pure) |
| `connection.py` | opens a connection from a config entry's data (TLS context, certificate pin) |
| `config_flow.py` | setup, reauth, reconfigure and the options flow |
| `__init__.py` | entry setup and unload, router device, disabling the entities of unselected features |
| `device_tracker.py` | one tracker per leased host, forgetting stale ones |
| `entity.py`, `entity_ids.py`, `sensor.py`, `binary_sensor.py`, `update.py` | the optional entities and their unique ids |

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
7. A rejected login (or a changed pinned certificate) stops the loop and starts the Home Assistant reauth flow.
8. Every command has a 15 s limit. A table read that times out ends the session and is reconnected, instead
   of leaving every tracker unavailable; an optional reading that times out is dropped on its own.

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

## Pushed readings (measured on RouterOS 7.25.1, RB5009)
Home Assistant opens the one connection and subscribes; the router then pushes down it.
| Reading | Mechanism | Cost |
|---|---|---|
| Link state of ports | `/interface/listen` (counters do not trigger events) | events on change only |
| Negotiated speed | `/interface/ethernet/monitor once`, asked after a link change | one call per change |
| Traffic rx/tx per port | `/interface/monitor-traffic interval=5s`, one command for all ports | router maximum interval is 5 s |
| CPU, free memory | `/system/resource/monitor interval=5s` | |
| Temperatures, voltages | `/system/health/print` every 60 s (no stream exists) | one call per minute |
| RouterOS version | `/system/package/update/print` every 6 h (needs no write permission) | |
| Active uplink | `/ip/route/print ?dst-address=0.0.0.0/0` every update interval; the active route of the main table (a table can be huge, so it is not streamed and the router does the filtering) | one small call per interval |
The router accepts intervals of 20 ms to 5 s only; Home Assistant coalesces the 5 s samples and announces
them at most once per update interval, so entities are not written more often than that.
Measured cost with every port streaming at 5 s: router CPU 5.8 % with and without, 1.7 KB/s.
Ports are identified by their factory name (`default-name`), so renaming one in WinBox keeps its entities
and history; the traffic stream restarts when the set of port names changes. Entities of a port that no
longer exists become unavailable and stay until deleted by hand. A failing optional reading (no permission, a
firmware without that command) is logged and dropped on its own; it never ends the session presence
depends on. Readings are cleared when a new connection starts, because the router may have rebooted.
The port list is re-read every 5 minutes like the presence tables (a speed change without a link change
is not noticed until the next link change). Switching a feature off disables its entities (disabled by the
config entry) instead of deleting them, so names, areas and dashboards survive switching it on again.
Switches (NAT, filter, queues), kid control and scripts are not offered: they need write access.

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
- MAC randomisation creates a new tracker per random MAC. Trackers Home Assistant disabled by default
  (nobody named the host) are deleted at a reconnect when the host has no lease and the entry was not
  touched for 30 days (the registry keeps no "last seen" time; a returning host gets its tracker back, disabled as before).
- Certificate verification only works with a certificate the system trusts; the generated self-signed
  certificate needs "verify" off. The fingerprint of the certificate seen on the first successful connection
  is pinned (stored in the entry; entries from before pinning existed get theirs at their next start). A
  different certificate is refused before the login is sent. Reauth then shows the new fingerprint and
  trusts it only after a second submit; a reauth for a rejected password keeps the pin. The very first
  connection is not authenticated (trust on first use). A certificate that changes by itself (for example
  an ACME one on api-ssl) needs that confirmation at every renewal: use the generated CA-signed one.
- The 180 s grace and 5 min resync are constants, not options.
- Not yet measured: events at AP reboot / port flap and sleeping phones over a long period.
- Departure is slow by design (about 5-8 min): the bridge ageing time plus the grace.
- The router user needs read access to both tables; otherwise setup reports `router_error`.

## Roadmap
1. Long capture on the real router (AP reboot, sleeping phones) and tune the grace.
2. Options for the grace and for which VLANs/bridges to track, diagnostics, a minimum RouterOS version
   check, the Wi-Fi registration table as a faster signal.
3. Traffic on PPPoE/VLAN/WireGuard interfaces, a RouterBOARD firmware `update` entity; netwatch webhook push.
4. Release tag equal to the manifest version, submit to `hacs/default`.
