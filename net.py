####################################################################################
# Network layer: the radio, the WiFi link, and HTTP requests (E2).
#
# Owns the WLAN interface, connecting and bouncing the link, do_request with
# its timeout and backoff, and the wedged-stack detection that decides when
# the link is up but carrying nothing.
#
# Dependencies: config, applog, wdt, led, and resets (a leaf) for the reason
# codes. Nothing above. Two things this module must trigger live above it,
# so main injects them once at import, the same way it sets applog.utcOffset:
#
#   on_give_up(reason, code)  reset deliberately. main's self_reset, which
#                             also snapshots the ring queue first.
#   on_wait()                 run once per pass while connect_wifi() blocks.
#                             main's maybe_snapshot_queue, so queued rings
#                             reach flash during a long outage.
#
# Both start as None on purpose: an unwired hook fails loudly rather than
# letting a reset or a snapshot silently not happen. Credentials (ssid, pw)
# are injected the same way, so main stays the single reader of secrets.py.
####################################################################################

import rp2
import network
import time
import gc
import urequests as requests
import config
import applog
import wdt
import led
import resets

# Newer builds name this; older ones only take the magic number.
WIFI_PM_NONE = getattr(network.WLAN, 'PM_NONE', 0xa11140)
WIFI_PM_PERFORMANCE = getattr(network.WLAN, 'PM_PERFORMANCE', 0xa11142)

# Set country to avoid possible errors
rp2.country('DE')

wlan = network.WLAN(network.STA_IF)

# Credentials, set by main at import.
ssid = ''
pw = ''

# Hooks into the layers above, set by main at import. See the header.
on_give_up = None
on_wait = None

# Wifi connection status: the last "WiFi connected. IP: ..." line. Read by the
# heartbeat and used to seed a cleared log.
wifiData = ''

def power_up(powerSave):
    """Power the radio and apply the power-save choice.

    Must run before anything network-related, and before the LED exists:
    on the Pico W the onboard LED hangs off the CYW43 chip. The choice is
    board policy (wifiPowerSave in board.py), passed in by the caller.
    """
    wlan.active(True)
    # Before connecting: the setting applies to the association.
    mode = WIFI_PM_PERFORMANCE if powerSave else WIFI_PM_NONE
    try:
        wlan.config(pm=mode)
        print('WiFi power save ' + ('on' if powerSave else 'off'))
    except Exception as e:
        # Not fatal. An unconfigurable radio still answers the door.
        print('Could not set WiFi power mode: ' + str(e))

def mac_address():
    """The radio's MAC, read from the interface we already have."""
    return wlan.config('mac')

####################################################################################
# B4 reconnection.
#
# The previous loop called wlan.connect() every three seconds for as long as the
# network was down. A ten-minute outage on the bench issued roughly 200 of them,
# and afterwards the board associated -- status 3, IP printed -- but passed no
# traffic at all. Re-issuing a connect while one is already in progress is a
# known way to wedge the cyw43 stack, and a wedged stack is precisely what a
# reset fixes.
####################################################################################


# Statuses meaning "a join is under way, leave it alone". MicroPython exposes
# no STAT_ constant for 2, which is associated-but-awaiting-DHCP.
WIFI_PROGRESS_STATES = (1, 2)

# The port's own names are thin and, for -3, misleading: cyw43 reports it for
# any association rejection, including MAC filtering or an AP block, not only
# a wrong password.
WIFI_STATUS_NAMES = {
    0: 'idle',
    1: 'joining',
    2: 'awaiting IP',
    3: 'connected',
    -1: 'link failed',
    -2: 'no AP found',
    -3: 'auth rejected',
}

# Applies per socket operation, not per request: DNS, connect, the TLS
# handshake and the read each get their own budget. A request can therefore
# outlast the 8 s watchdog even with a timeout set, which is exactly what
# happened on the bench -- a reset landing immediately after
# 'HTTP GET failed: ETIMEDOUT'.
#
# Five seconds is a deliberate choice, not a default. Three was tried after a
# run of ETIMEDOUT failures, but those were most likely the inter-VLAN hop and
# WiFi power save rather than anything the timeout could fix -- both since
# removed. Tuning against a problem that is being eliminated only leaves a
# margin tighter than the hardware needs, and turns healthy-but-slow requests
# into retries.
#
# The exposure is reduced, not removed: getaddrinfo can block outside the
# timeout entirely, and the RP2040 caps the watchdog near 8.3 s, so there is no
# headroom to buy on the other side. The answer to that is to make a reset
# cheap, which the flash boot counter, the scratch reset reason and the
# immediate queue snapshot already do.
# config.REQUEST_TIMEOUT_S now lives in config.py.

# Flipped off the first time urequests rejects the timeout argument.
requestsTimeoutSupported = True

####################################################################################
# Wedged-stack detection.
#
# Reproduced on the bench: after the AP rejected the board for a few minutes,
# it re-associated -- wlan.status() reported connected and ifconfig printed an
# IP -- but every DNS lookup returned -2, indefinitely. B4's attempt counter
# does not help, because it only guards the connection phase; once status
# reads 3 the reset path is out of reach.
#
# A stack that claims to be up and passes nothing is worse than one that admits
# it is down, because nothing notices.
#
# The remedy escalates rather than jumping to a reset, because the firmware
# cannot tell a wedged local stack from an upstream block. A router that filters
# a device while leaving association and DHCP intact produces exactly the same
# symptom -- and did, during testing, which is why the original diagnosis here
# is uncertain. Resetting cannot fix an upstream block, so the cheap local
# remedy comes first:
#
#   1. bounce the WiFi link (disconnect, reconnect)
#   2. if failures continue after that, reset
#
# The bounce also preserves the RAM log, which a reset destroys.
####################################################################################

# Elapsed time without a success, not a failure count. Backoff stretches a
# count into an unpredictable duration -- fifteen failures works out at about
# twelve minutes, by which point the AP had already deauthenticated the board
# on the bench and this path was unreachable.
#
# Five minutes, not two. An associated-but-dead link recovered on its own
# after roughly seven minutes on the bench, with no intervention: the cause
# was upstream, not local. Bouncing at two minutes would have discarded a
# working association several minutes before the network returned -- and
# reassociating on that router took 28 attempts. Nothing is lost by waiting,
# because the queue holds the ring; acting early can make recovery slower.
# Network dead/backoff timings now live in config.py.

networkFailures = 0
networkBackoffMs = 0
networkRetryAt = 0
networkBounceRequested = False
networkBounced = False
lastNetworkSuccess = 0

REQUEST_OK = 0          # 2xx, body decoded
REQUEST_RETRY = 1       # transient (network fault or 5xx), safe to retry
REQUEST_RATE_LIMIT = 2  # 429, honour retry_after before retrying
REQUEST_FATAL = 3       # other 4xx, retrying will not help
REQUEST_SKIPPED = 4     # never attempted: backoff, or the link is down

def classify_response(status):
    if status <= 0:
        # No HTTP response at all -- transient by definition.
        return REQUEST_RETRY
    if status >= 200 and status < 300:
        return REQUEST_OK
    if status == 429:
        return REQUEST_RATE_LIMIT
    if status >= 500:
        return REQUEST_RETRY
    return REQUEST_FATAL

def note_request_result(outcome):
    """Track whether the network is actually carrying traffic.

    A run of failures while `wlan.status()` still reports a connection means
    the stack is associated but dead. Nothing else detects that state, and
    only a reset clears it.
    """
    global networkFailures, networkBackoffMs, networkRetryAt
    global networkBounceRequested, networkBounced, lastNetworkSuccess
    now = time.ticks_ms()
    if outcome == REQUEST_OK:
        if networkFailures:
            applog.report('Network recovered after ' + str(networkFailures) +
                   ' failure(s)')
        networkFailures = 0
        networkBackoffMs = 0
        networkBounced = False
        lastNetworkSuccess = now
        return
    if outcome in (REQUEST_FATAL, REQUEST_SKIPPED):
        # FATAL: Telegram answered, so the link is fine. SKIPPED: nothing was
        # attempted, so it is evidence of nothing.
        return
    networkFailures += 1
    if networkBackoffMs:
        networkBackoffMs = min(networkBackoffMs * 2, config.NETWORK_BACKOFF_MAX_MS)
    else:
        networkBackoffMs = config.NETWORK_BACKOFF_MS
    networkRetryAt = time.ticks_add(now, networkBackoffMs)
    if (time.ticks_diff(now, lastNetworkSuccess) > config.NETWORK_DEAD_MS and
            is_wifi_connected()):
        if networkBounced:
            # The link was already bounced and it did not help, so the fault
            # is not the association. Either the stack is wedged below it, or
            # the problem is upstream and a reset will not fix that either --
            # but a reset is the only local action left.
            on_give_up('network dead for ' +
                       str(time.ticks_diff(now, lastNetworkSuccess) // 1000) +
                       's after a WiFi bounce', resets.REASON_NETWORK)
        else:
            # Requested rather than done here: this runs deep inside a
            # request, and reconnecting from there would be re-entrant.
            networkBounceRequested = True

def bounce_wifi():
    """Drop and re-establish the link, without resetting the board.

    The gentler half of the escalation. Clears an association that is up
    but carrying nothing, and unlike a reset it keeps the log, the queued
    rings in RAM, and the uptime.
    """
    global networkBounceRequested, networkBounced
    global networkFailures, networkBackoffMs
    networkBounceRequested = False
    networkBounced = True
    networkFailures = 0
    networkBackoffMs = 0
    applog.report('Network unresponsive while associated; bouncing the link')
    try:
        wlan.disconnect()
    except Exception as e:
        applog.report('WiFi disconnect failed: ' + str(e))
    wdt.sleep_fed(2)
    connect_wifi()

def do_request(method, url, payload=None):
    """Perform an HTTP request and always release the socket.

    Returns (outcome, status, body), where body is the decoded JSON
    response or None. Never raises for network or HTTP-level failures --
    callers branch on outcome instead.
    """
    global requestsTimeoutSupported
    # Do not open a socket the network cannot carry. Losing WiFi mid-request
    # is what hangs urequests, and the check is free.
    if not is_wifi_connected():
        return (REQUEST_SKIPPED, 0, None)

    # Back off after failures rather than retrying every pass. Without this a
    # long outage burns hundreds of DNS lookups an hour and floods the log.
    if networkBackoffMs and time.ticks_diff(time.ticks_ms(), networkRetryAt) < 0:
        # Not a failure: nothing was attempted. Logging it as one produced
        # entries like 'getUpdates failed: status=0' for requests that never
        # happened, and counted against the dead-network deadline twice.
        return (REQUEST_SKIPPED, 0, None)

    response = None
    # A handshake can run into seconds; the watchdog must not bite mid-request.
    wdt.feed_watchdog()
    try:
        if requestsTimeoutSupported:
            try:
                if method == 'POST':
                    response = requests.post(url, json=payload,
                                             timeout=config.REQUEST_TIMEOUT_S)
                else:
                    response = requests.get(url, timeout=config.REQUEST_TIMEOUT_S)
            except TypeError:
                # This urequests build has no timeout parameter. Note it once
                # and fall through to the untimed call below.
                requestsTimeoutSupported = False
                applog.append_to_log('urequests has no timeout support; '
                              'the watchdog is the only backstop')
        if response is None and not requestsTimeoutSupported:
            if method == 'POST':
                response = requests.post(url, json=payload)
            else:
                response = requests.get(url)
        status = response.status_code
        # Decode while the socket is still open. Telegram answers JSON
        # for errors too, so this is also how we read error details.
        try:
            body = response.json()
        except (ValueError, OSError):
            body = None
        outcome = classify_response(status)
        note_request_result(outcome)
        return (outcome, status, body)
    except OSError as e:
        # DNS failure, refused connection, TLS failure, timeout. Printed,
        # not just logged: when the network misbehaves this errno is the
        # first thing anyone needs, and a full log used to swallow it.
        note_request_result(REQUEST_RETRY)
        # The backoff is stated because it is otherwise invisible: requests
        # skipped while it runs print nothing at all, so a quiet log looks
        # the same whether nothing was tried or nothing failed.
        applog.report('HTTP ' + method + ' failed: ' + str(e) +
               ' (next attempt in ' + str(networkBackoffMs // 1000) + 's)')
        return (REQUEST_RETRY, 0, None)
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                # Closing must never mask the real outcome.
                pass
        # urequests leaks sockets quickly without this on a 264 KB part.
        gc.collect()
        wdt.feed_watchdog()

def is_wifi_connected():
    wlan_status = wlan.status()
    if wlan_status != 3:
        return False
    else:
        return True

def wifi_status_name(code):
    return WIFI_STATUS_NAMES.get(code, 'status ' + str(code))

def connect_wifi():
    """Bring the WiFi link up. Connection only -- no notifications.

    Callers decide whether to announce; mixing the two made a transport
    failure look like a connection failure.

    One connect() is issued and then given config.WIFI_REISSUE_MS to work before
    another is tried. The previous code re-issued every three seconds,
    which left the stack associated but unable to pass traffic after a long
    outage. After config.WIFI_MAX_ATTEMPTS the board resets, because at that point
    a wedged driver is the likeliest remaining explanation and a reset is
    the only thing that clears it.
    """
    global wifiData, lastNetworkSuccess
    attempts = 0
    issuedAt = None
    while True:
        if (is_wifi_connected()):
            led.blink_led(3)
            led.set_led_state(led.LED_CONNECTED)
            status = wlan.ifconfig()
            print('ip = ' + status[0])
            wifiData = 'WiFi connected. IP: ' + status[0]
            applog.append_to_log(wifiData)
            # A fresh link deserves a fresh grace period; otherwise the first
            # failure after a long outage looks like a dead network.
            lastNetworkSuccess = time.ticks_ms()
            return True

        now = time.ticks_ms()
        status = wlan.status()
        if issuedAt is None:
            reissue = True
        elif status in WIFI_PROGRESS_STATES:
            # A join is under way. Calling connect() again aborts it and
            # starts over -- observed as seven attempts over three and a
            # half minutes on a network that was perfectly available.
            reissue = time.ticks_diff(now, issuedAt) > config.WIFI_PROGRESS_MAX_MS
        else:
            reissue = time.ticks_diff(now, issuedAt) > config.WIFI_REISSUE_MS

        if reissue:
            attempts += 1
            if attempts > config.WIFI_MAX_ATTEMPTS:
                on_give_up('WiFi unreachable after ' + str(attempts - 1) +
                           ' attempts', resets.REASON_WIFI)
            led.set_led_state(led.LED_CONNECTING)
            applog.report('WiFi down (' + wifi_status_name(status) +
                   '), attempt ' + str(attempts))
            try:
                wlan.connect(ssid, pw)
            except OSError as e:
                applog.report('WiFi connect failed: ' + str(e))
            issuedAt = now

        wdt.sleep_fed(config.WIFI_POLL_MS / 1000.0)
        # Connecting blink: one toggle per poll pass gives a ~1 Hz flash
        # while the join is under way, without a timer. Only while the state
        # is connecting, so a caller that set some other state is respected.
        if led.ledState == led.LED_CONNECTING:
            led.led_toggle()
        # The loop is blocked here for as long as the outage lasts, so the
        # queue would otherwise never reach flash during the one situation
        # it exists for. The queue lives above this module, so the caller's
        # hook does the snapshot.
        on_wait()

def wifi_rssi():
    try:
        return str(wlan.status('rssi')) + ' dBm'
    except Exception:
        return 'unknown'
