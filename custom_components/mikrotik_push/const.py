"""Constants for MikroTik Push."""

DOMAIN = "mikrotik_push"
DEFAULT_PORT = 8729  # api-ssl; plain api is 8728 (cleartext password)
IDENTITY_PATH = "/system/identity/print"  # cheap read; also used as the keepalive probe
