"""Constants for the PeekESP integration."""

DOMAIN = "peekesp"

# Where a freshly flashed device looks unless told otherwise (PeekESP.ino).
DEFAULT_RELAY_BASE = "https://peek-relay.peekesp.workers.dev"

CONF_URL = "url"
CONF_TOKEN = "token"
CONF_CODE = "code"
CONF_RELAY_BASE = "relay_base"
CONF_MODE = "mode"
MODE_PAIRED = "paired"
MODE_MANUAL = "manual"

CONF_SCAN_INTERVAL = "scan_interval"
CONF_OFFLINE_AFTER = "offline_after"

# The device polls every 5 s by default. HA polls the same endpoint, so every
# second here is a request against the same Worker quota.
DEFAULT_SCAN_INTERVAL = 10
MIN_SCAN_INTERVAL = 5
# The firmware calls a machine offline after poll_s * 5 (25 s at defaults).
DEFAULT_OFFLINE_AFTER = 30

CARD_URL = "/peekesp_static/peekesp-card.js"
