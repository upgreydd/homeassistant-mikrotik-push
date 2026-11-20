# Review before pasting into a RouterOS 7 terminal. Replace HA_IP and the password.
# Nothing here reboots or flushes anything; every line is reversible.

# 1. Read-only group. Built-in `read` also grants sensitive, reboot, ssh, winbox, web, ... we do not want that.
/user/group add name=ha-readonly policy=read,api comment="Home Assistant (MikroTik Push)"

# 2. User, allowed to log in only from the HA host.
/user add name=ha group=ha-readonly password="CHANGE-ME-LONG-RANDOM" address=HA_IP/32 comment="Home Assistant"

# 3. API transport. Prefer api-ssl (8729): plain api (8728) sends the password in cleartext.
#    Optional self-signed certificate so HA can verify the router (otherwise leave "verify" off in HA):
/certificate add name=api-ssl common-name=router.lan days-valid=3650 key-usage=tls-server
/certificate sign api-ssl
/ip/service set api-ssl certificate=api-ssl address=HA_IP/32 disabled=no
#    Plain API is not needed once api-ssl works:
# /ip/service set api disabled=yes
