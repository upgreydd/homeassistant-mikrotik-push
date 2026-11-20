"""Constants for MikroTik Push."""

DOMAIN = "mikrotik_push"
DEFAULT_PORT = 8729  # api-ssl; plain api is 8728 (cleartext password)
IDENTITY_PATH = "/system/identity/print"  # cheap read; also used as the keepalive probe

CONF_TRACK_PORTS = "track_ports"
CONF_TRACK_TRAFFIC = "track_traffic"
CONF_TRACK_SYSTEM = "track_system"
CONF_UPDATE_INTERVAL = "update_interval"
DEFAULT_UPDATE_INTERVAL = 30  # seconds between state writes of fast-changing values
MIN_UPDATE_INTERVAL = 5  # the router cannot push faster than every 5 s anyway
MAX_UPDATE_INTERVAL = 300
