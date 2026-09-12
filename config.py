####################################################################################
# Tunable policy: timings, thresholds, and message strings (E1, E2).
#
# The numbers and text a maintainer might reasonably adjust, gathered in one
# place so tuning does not mean hunting through the logic. Pure by design: this
# module imports nothing, so every other module can depend on it without a
# cycle. Structural constants that are part of one module's implementation
# (field indices, register addresses, request-outcome enums) stay with that
# module; they are not configuration.
####################################################################################

# --- Messages -------------------------------------------------------------
startupText = 'I am online for the first time! Bot started!'
reconnectText = 'I am back online! Bot reconnected!'
text = 'Doorbell activated!'

# --- Commands -------------------------------------------------------------
logCommand = '/log'
statusCommand = '/status'

# --- Heartbeat and log ----------------------------------------------------
HEARTBEAT_MS = 21600000      # six hours: four a day, not chatty
logCheckInterval = 60000
LOG_MAX_LINES = 120          # bounded by count, not characters
LOG_CHUNK_CHARS = 3500       # under Telegram's 4096 limit, with room for markup

# --- C1 wall clock --------------------------------------------------------
# MicroPython's epoch is 2000-01-01, not Unix's 1970-01-01. ntptime.time()
# and time.gmtime() both use it, so no conversion is needed between them; the
# sanity floor below is written in the Unix epoch for readability and shifted
# into the MicroPython epoch once here.
EPOCH_2000_OFFSET = 946684800
NTP_TIMEOUT_S = 2
NTP_RESYNC_MS = 43200000     # twelve hours; tighter than drift needs, cheap
EPOCH_SANITY_FLOOR = 1577836800          # 2020-01-01 UTC, Unix epoch
EPOCH_SANITY_FLOOR_MP = EPOCH_SANITY_FLOOR - EPOCH_2000_OFFSET

# --- WiFi reconnection timings --------------------------------------------
WIFI_POLL_MS = 500           # how often to check status while waiting
WIFI_REISSUE_MS = 10000      # a stalled attempt retries sooner than a live one
WIFI_PROGRESS_MAX_MS = 20000 # a join in progress gets this long before reissue
WIFI_MAX_ATTEMPTS = 30       # then reset rather than keep flailing

# --- Watchdog and loop ----------------------------------------------------
WDT_TIMEOUT_MS = 8000        # under the RP2040 ~8.3 s ceiling
BOOT_STABLE_MS = 60000       # uptime before a boot counts as stable
loopDelay = 1

# --- B1 input timings -----------------------------------------------------
DEBOUNCE_MS = 50             # ignore further edges this soon after one
MIN_PULSE_MS = 150           # shorter than this is a transient, not a visitor
ALERT_LOCKOUT_MS = 5000      # one alert per ring; must exceed the pulse width
STUCK_INPUT_MS = 15000       # held longer than this is a fault, not a caller
pressed = 1                  # a ring drives 5 V, so a press is a rising edge

# --- B6 undelivered-ring queue --------------------------------------------
QUEUE_MAX = 20                    # bounded: a long outage must not exhaust RAM
QUEUE_SNAPSHOT_AFTER_MS = 300000  # only persist once an outage passes 5 minutes
QUEUE_SNAPSHOT_MIN_MS = 300000    # and never more than once per 5 minutes
QUEUE_FLUSH_PER_PASS = 3          # bound the work in any one loop pass
DELAY_NOTICE_MS = 10000           # say so if a ring is delivered this late

# --- Network backoff and dead-stack detection -----------------------------
REQUEST_TIMEOUT_S = 5             # per socket operation, not per request
NETWORK_DEAD_MS = 300000          # elapsed without success before escalating
NETWORK_BACKOFF_MS = 5000         # first retry delay, doubling
NETWORK_BACKOFF_MAX_MS = 60000
