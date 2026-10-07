"""Constants and protocol for the TC66C (protocol taken from rd-usb, interfaces/tc.py, GPL-3.0)."""

DOMAIN = "tc66c"
CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL = 5  # seconds
MIN_SCAN_INTERVAL = 2
MAX_SCAN_INTERVAL = 3600
RESPONSE_TIMEOUT = 5.0
MAX_SOFT_FAILURES = 3  # unavailable only after this many failed readings in a row

# Charge sessions
CONF_CHARGE_THRESHOLD = "charge_threshold"   # A: from this current on it counts as charging
DEFAULT_CHARGE_THRESHOLD = 0.10
CONF_END_DELAY = "end_delay"                 # s: this long below the threshold = charging finished
DEFAULT_END_DELAY = 60
EVENT_CHARGING_FINISHED = "tc66c_charging_finished"
CONF_RETRY_MINUTES = "retry_minutes"         # min: retry even without a new advertisement (0 = off)
DEFAULT_RETRY_MINUTES = 5
RESUME_WINDOW_S = 900  # s: after an HA restart a session continues if the meter reports again within this time

# Two variants of the BLE module: (write characteristic, notify characteristic)
CHAR_PAIRS = (
    ("0000ffe9-0000-1000-8000-00805f9b34fb", "0000ffe4-0000-1000-8000-00805f9b34fb"),
    ("0000ffe2-0000-1000-8000-00805f9b34fb", "0000ffe1-0000-1000-8000-00805f9b34fb"),
)
COMMAND = b"bgetva\r\n"
RESPONSE_LEN = 192

AES_KEY = bytes(
    x & 0xFF
    for x in (
        88, 33, -6, 86, 1, -78, -16, 38,
        -121, -1, 18, 4, 98, 42, 79, -80,
        -122, -12, 2, 96, -127, 111, -102, 11,
        -89, -15, 6, 97, -102, -72, 114, -120,
    )
)
