# Router setup (RouterOS 7)

What the integration needs from the router, nothing more.

## 1. A read-only API user (required)

Paste `docs/router/setup.rsc` (after replacing `HA_IP` and the password), or by hand:

```
/user/group add name=ha-readonly policy=read,api
/user add name=ha group=ha-readonly password="..." address=<HA_IP>/32
```

Why a new group: the built-in `read` group also contains `sensitive`, `reboot`, `ssh`, `winbox`, `web`
and more. `read,api` is enough for `print`, `listen` and the system queries we use (to be confirmed on your
router in milestone 1; if RouterOS answers "not enough permissions (9)" we add exactly the missing policy).
The `address=` restriction means the account is useless from anywhere except the HA host.

## 2. API service (required)

| Service | Port | Use |
|---|---|---|
| `api-ssl` | 8729 | recommended; needs a certificate to be verifiable (the `.rsc` makes a self-signed one) |
| `api` | 8728 | works, but the password travels in cleartext; restrict by `address=` |

`/ip/service print` shows the state. Also allow the port in the firewall input chain from HA only.

## Verified on the real router (2026-11-20, RB5009UG+S+, RouterOS 7.25.1)
- Login with a group of `read,api` (the live group also had `reboot,policy,test`, which are NOT needed).
- `print` works for identity, resource, routerboard, package, health, dhcp-server, dhcp lease, arp.
- `listen` is accepted (no trap) on `/ip/dhcp-server/lease`, `/ip/arp`, `/interface`,
  `/interface/bridge/host`, `/ip/neighbor`. This router has no Wi-Fi (`/interface/wifi` is empty).
- api-ssl (8729) fails the TLS handshake while no certificate is assigned; use the certificate step in
  `setup.rsc` or the plain API (8728) on a trusted LAN.

## 3. Optional, later milestones (not needed for milestone 1)

- WAN / host up-down push: a `/tool/netwatch` entry with `up-script`/`down-script` that POSTs to an HA
  webhook with `/tool/fetch`. The script runs with its own policy list, not the API user's. Which policies
  fetch needs is not stated in the official docs (sources disagree on `ftp`), so it will be verified on the
  real router before we document it.
- Router logs via remote syslog to an HA-side UDP listener.

## What I need from you to start milestone 1

1. Run the `.rsc`, then give me the host, port, user and password (via a secret, not chat if you can avoid it).
2. Output of `/system/resource print`, `/system/package print`, and `/interface/wifi print` or
   `/interface/wireless print` (whichever exists), so I know the RouterOS version and wifi stack.
