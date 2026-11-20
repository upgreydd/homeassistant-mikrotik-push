# Router setup (RouterOS 7)

## The easy way: let the integration generate the commands
Add the integration, enter the router's address, and the next screen shows a ready-to-paste script with a
**one-time random password** already filled into the form. Paste it into a RouterOS terminal (WinBox: New
Terminal), then press Submit. The script:

- creates (or updates) the group `mikrotik-push` with **only `read` and `api`** (the built-in `read` group
  also has `sensitive`, `reboot`, `ssh`, `winbox`, `web`...);
- creates (or updates) the user `mikrotik-push`, allowed to log in **only from the Home Assistant address**;
- creates a local CA (`mikrotik-push-ca`) and a server certificate signed by it, assigns that to the
  `api-ssl` service (port 8729), but only when it has not created a working one before, so an existing
  certificate setup is never overwritten (a half-finished attempt of ours is removed and redone);
- enables `api-ssl`;
- adds a scheduler task `mikrotik-push-update-check` (once a day, plus one run when you paste the
  script) so the router learns of new RouterOS versions; the Home Assistant `update` entity shows the result.
  The task runs with its own policies (`read,write,test`), not with the API user's, which stays `read,api`.

Pasting it again rotates the password. When the router rejects the login later (password changed, user
removed), Home Assistant starts a re-authentication that shows a fresh script and password. The policies it uses are enough for `print` and `listen`
(verified on RouterOS 7.25.1); if RouterOS answers "not enough permissions (9)" the integration reports
`router_error` and says which user to check.

## What you still do yourself
- **Firewall:** allow TCP 8729 (or 8728) to the router from Home Assistant in the `input` chain.
- **Static address for Home Assistant:** the user is bound to its IP; if the address changes, logins fail and
  Home Assistant asks for the password again.
- **Plain API:** `/ip service set api disabled=yes` once nothing else uses port 8728 (it sends passwords in
  clear text).

## TLS notes
- api-ssl needs a certificate; without one the handshake fails.
- The default is "TLS on, verify off": the traffic is encrypted but the router is not authenticated, so an
  attacker who can intercept LAN traffic could capture the one-time password. Certificate pinning is planned.
- "Verify" only works with a certificate the Home Assistant system trusts.

## Credentials at rest
Home Assistant stores the password in `.storage/core.config_entries` and in backups. That is why the user
has read-only rights and a source-address restriction.
