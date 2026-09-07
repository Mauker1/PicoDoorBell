####################################################################################
# MIT License
#
# Copyright (c) 2022 Maurício C. P. Pessoa
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
####################################################################################

import rp2
import network
import ubinascii
import machine
import urequests as requests
import time
import gc
# MicroPython exposed these only as ujson/uos before v1.20; the fallback
# keeps the file working on older builds and under host-side CPython.
try:
    import ujson as json
except ImportError:
    import json
try:
    import uos as os
except ImportError:
    import os
from secrets import secrets

####################################################################################
# Board definition. Single source of truth for pin assignments -- main.py holds no
# defaults, so a board it cannot identify is a board it refuses to drive.
#
# Copy the file matching your carrier revision from boards/ to `board.py` on the
# device. Failing at import is deliberate: a Pico with no pin definition cannot
# watch the doorbell, and booting into something that looks alive but is not is the
# exact silent failure this firmware exists to avoid.
####################################################################################

REQUIRED_BOARD_PINS = ('doorBellPin',)

try:
    import board
except ImportError:
    raise ImportError(
        'No board.py found. Copy the definition for your carrier revision from '
        'boards/ (for example boards/board_v1_2.py) to board.py on the device.')

_missing = []
for _name in REQUIRED_BOARD_PINS:
    if not hasattr(board, _name):
        _missing.append(_name)
if _missing:
    raise ValueError(
        'board.py is incomplete, missing: ' + ', '.join(_missing) +
        '. Compare it against the files in boards/.')

doorBellPin = board.doorBellPin

# B8: WiFi power save. Off for a mains-powered unit -- the CYW43439 otherwise
# sleeps between beacons, which adds latency and drops packets, and is the
# standard explanation for the timeouts an always-on device sees.
#
# Optional in board.py rather than required, so existing installs keep working.
# The future battery build (G5) is the one case that wants it on: there the
# tradeoff inverts and a few dropped packets are cheaper than the current draw.
wifiPowerSave = getattr(board, 'wifiPowerSave', False)

# Newer builds name this; older ones only take the magic number.
WIFI_PM_NONE = getattr(network.WLAN, 'PM_NONE', 0xa11140)
WIFI_PM_PERFORMANCE = getattr(network.WLAN, 'PM_PERFORMANCE', 0xa11142)

# Set country to avoid possible errors
rp2.country('DE')

wlan = network.WLAN(network.STA_IF)

# Configured by setup_hardware() during boot, not at import time, so that
# failures are catchable and the ordering is explicit.
led = None
doorBellInput = None
mac = ''

# Load login data from different file for safety reasons
ssid = secrets['ssid']
pw = secrets['pw']
botToken = secrets['botToken']
chatId = secrets['telegramDmUid']

# Messages
startupText = 'I am online for the first time! Bot started!'
reconnectText = 'I am back online! Bot reconnected!'
text = 'Doorbell activated!'

# Commands
logCommand = "/log"
statusCommand = "/status"

# In memory log
# Kept as a list of lines, never as one growing string. `log += entry`
# reallocates the whole buffer on every append; a few hundred appends during
# an outage means a few hundred multi-kilobyte allocations and frees, which
# fragments a 264 KB heap. TLS handshakes need large contiguous blocks, so
# the symptom is sends beginning to fail while everything small still works.
logLines = []

# Wifi connection status
wifiData = ''

# Telegram update id for offset
updateId = 0

# Flags
isStartup = True

# An announcement that has not been delivered yet. The startup one carries the
# reset diagnosis, which is the whole point of C4 on the production unit -- a
# reboot during a brief network blip must not lose it.
pendingAnnouncement = None

####################################################################################
# C3 heartbeat.
#
# The device's only "still alive" signal was a per-minute console print that
# said nothing and that nobody watches. Silence and death looked identical.
#
# A periodic report carries the numbers that only uptime can produce -- free
# memory above all, since the socket-leak fix can be proven no other way, and
# reading it by hand means killing the run to reach a REPL.
####################################################################################

HEARTBEAT_MS = 21600000      # six hours: four a day, not chatty

lastHeartbeat = 0
# Accumulated rather than derived from ticks_ms, which wraps at ~12.4 days and
# is ambiguous past half of that. Summing per-pass differences is wrap-safe
# for as long as the device runs.
uptimeMs = 0

# Time variables
startupTime = time.ticks_ms()
lastLogCheck = startupTime
logCheckInterval = 60000
LOG_MAX_LINES = 120       # bounded by count, not characters
LOG_CHUNK_CHARS = 3500    # under Telegram's 4096 limit, with room for markup

####################################################################################
# B2 watchdog.
#
# The RP2040 watchdog cannot exceed roughly 8.3 s, which is uncomfortably close to
# what one loop pass can legitimately take. Measured on hardware: a Telegram round
# trip is 1-2 s, and a worst-case pass can hold a send, a getUpdates and a flash
# sector erase.
#
# The margin is therefore bought by feeding from inside the blocking work rather
# than only at the top of the loop -- see feed_watchdog() call sites. B1 was a
# prerequisite: with its 5 s post-press sleep still in place, a press followed by
# a getUpdates could pass nine seconds without a feed.
#
# Once armed, an RP2040 watchdog cannot be disarmed.
####################################################################################

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

WIFI_POLL_MS = 500          # how often to check status while waiting
# A terminal status means the attempt is over, so waiting long achieves
# nothing -- but retrying instantly is the hammering this function exists to
# avoid. Must stay below WIFI_PROGRESS_MAX_MS: a dead attempt should retry
# sooner than a live one, not later.
WIFI_REISSUE_MS = 10000
# A healthy association plus DHCP completes in a few seconds. Twenty leaves
# generous margin; sixty, tried first, wasted a minute per attempt on a
# network where DHCP was stalling anyway.
WIFI_PROGRESS_MAX_MS = 20000
WIFI_MAX_ATTEMPTS = 30      # then reset rather than keep flailing

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

WDT_TIMEOUT_MS = 8000

wdt = None

# Delays
loopDelay = 1

####################################################################################
# B1 input timings. The old code used one 5 s sleep for three different jobs --
# debounce, one-alert-per-ring, and an accidental rate limit. They are separate
# concerns with different right answers, so they are separate constants.
####################################################################################

# Ignore further edges this soon after one. Contact and optocoupler noise only.
DEBOUNCE_MS = 50
# A real ring holds terminal 04 high for about 2 s (measured). Anything
# shorter than this is a transient, not a visitor.
MIN_PULSE_MS = 150
# One alert per ring. Must exceed the pulse width so a single ring cannot
# produce two notifications.
ALERT_LOCKOUT_MS = 5000
# Held high far longer than any real ring: a fault, not a caller.
STUCK_INPUT_MS = 15000

# Button pressed value. Idle is ground, a ring drives 5 V through the
# optocoupler, so a press is a rising edge.
pressed = 1

####################################################################################
# Latched inputs.
#
# Records are plain lists, allocated once at setup. Interrupt handlers may not
# allocate under MicroPython, and assigning to an existing list slot does not.
# The list-of-records shape is also what lets a second input (G9, telling the
# building entrance from the flat door) become another entry rather than a
# rewrite -- though only one is wired today.
####################################################################################

IN_NAME = 0        # for log lines
IN_PIN = 1         # machine.Pin
IN_RISE = 2        # ticks_ms of the pending rising edge
IN_FALL = 3        # ticks_ms of the falling edge that closed it
IN_PENDING = 4     # a rising edge is waiting to be processed
IN_COMPLETE = 5    # its falling edge has arrived, so the width is known
IN_LAST_EDGE = 6   # debounce reference
IN_LAST_ALERT = 7  # lockout reference
IN_RINGS = 8       # accepted
IN_REJECTED = 9    # too short to be real
IN_MISSED = 10     # would have been lost by the old polling loop
IN_FIELDS = 11

####################################################################################
# B6 undelivered-ring queue.
#
# A ring detected during a network outage used to be logged and then lost.
# Rings now wait in RAM until they can be sent.
#
# RAM first: the queue exists to survive a *network* outage, which RAM covers
# completely. Flash only helps across a reset, so it is written only when an
# outage has already run long -- a Tier 3 write, event-driven and rare.
####################################################################################

QUEUE_MAX = 20                    # bounded: a long outage must not exhaust RAM
QUEUE_SNAPSHOT_AFTER_MS = 300000  # only persist once an outage passes 5 minutes
QUEUE_SNAPSHOT_MIN_MS = 300000    # and never more than once per 5 minutes
QUEUE_FLUSH_PER_PASS = 3          # bound the work in any one loop pass
DELAY_NOTICE_MS = 10000           # say so if a ring is delivered this late

Q_TICKS = 0      # ticks_ms when it was latched; meaningless across a reset
Q_WIDTH = 1      # pulse width, for the log
Q_EPOCH = 2      # wall-clock seconds, or None until C1 lands
Q_RESTORED = 3   # came back from flash, so its age is unknowable

queue = []
queueDropped = 0
lastSnapshotTicks = 0

inputs = []
# Start of the current main-loop pass, and of the one before it. Used to work
# out whether a ring landed in a window where polling could not have seen it.
lastPassTicks = 0
prevPassTicks = 0

# Telegram send message URL
sendURL = 'https://api.telegram.org/bot' + botToken + '/sendMessage'

# Telegram getUpdates URL
getURL = 'https://api.telegram.org/bot' + botToken + '/getUpdates'
    
# Request outcomes
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
REQUEST_TIMEOUT_S = 5

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
NETWORK_DEAD_MS = 300000
NETWORK_BACKOFF_MS = 5000    # first retry delay, doubling
NETWORK_BACKOFF_MAX_MS = 60000

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

def describe_api_error(status, body):
    # Telegram reports failures as
    # {"ok": false, "error_code": N, "description": "..."}
    detail = ''
    if body is not None:
        try:
            detail = ' ' + str(body.get('description', ''))
        except AttributeError:
            detail = ''
    return 'status=' + str(status) + detail

def retry_after(body):
    # Telegram puts the cooldown in parameters.retry_after on a 429.
    if body is None:
        return 0
    try:
        return int(body['parameters']['retry_after'])
    except (KeyError, TypeError, ValueError):
        return 0

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
            report('Network recovered after ' + str(networkFailures) +
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
        networkBackoffMs = min(networkBackoffMs * 2, NETWORK_BACKOFF_MAX_MS)
    else:
        networkBackoffMs = NETWORK_BACKOFF_MS
    networkRetryAt = time.ticks_add(now, networkBackoffMs)
    if (time.ticks_diff(now, lastNetworkSuccess) > NETWORK_DEAD_MS and
            is_wifi_connected()):
        if networkBounced:
            # The link was already bounced and it did not help, so the fault
            # is not the association. Either the stack is wedged below it, or
            # the problem is upstream and a reset will not fix that either --
            # but a reset is the only local action left.
            self_reset('network dead for ' +
                       str(time.ticks_diff(now, lastNetworkSuccess) // 1000) +
                       's after a WiFi bounce', REASON_NETWORK)
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
    report('Network unresponsive while associated; bouncing the link')
    try:
        wlan.disconnect()
    except Exception as e:
        report('WiFi disconnect failed: ' + str(e))
    sleep_fed(2)
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
    feed_watchdog()
    try:
        if requestsTimeoutSupported:
            try:
                if method == 'POST':
                    response = requests.post(url, json=payload,
                                             timeout=REQUEST_TIMEOUT_S)
                else:
                    response = requests.get(url, timeout=REQUEST_TIMEOUT_S)
            except TypeError:
                # This urequests build has no timeout parameter. Note it once
                # and fall through to the untimed call below.
                requestsTimeoutSupported = False
                append_to_log('urequests has no timeout support; '
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
        report('HTTP ' + method + ' failed: ' + str(e) +
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
        feed_watchdog()

def arm_watchdog():
    """Start the watchdog. Irreversible on this chip.

    Armed after hardware and state are up but before networking, because a
    wedged cyw43 stack is the failure this is chiefly for. Not armed any
    earlier: error_halt() blinks forever by design, and a watchdog would
    turn a configuration mistake into a silent reset loop instead of a
    visible fault.
    """
    global wdt
    if wdt is not None:
        return
    try:
        wdt = machine.WDT(timeout=WDT_TIMEOUT_MS)
        message = 'Watchdog armed at ' + str(WDT_TIMEOUT_MS) + 'ms'
    except Exception as e:
        # An unguarded device still answers the door. One that refuses to
        # start does not.
        message = 'Watchdog unavailable: ' + str(e)
    report(message)

def feed_watchdog():
    if wdt is not None:
        try:
            wdt.feed()
        except Exception:
            pass

def sleep_fed(seconds):
    """Sleep without letting the watchdog bite.

    Any wait longer than the timeout has to be broken up. The 10 s grace
    period after an error was the clearest example: left whole, it would
    have reset the device every time anything went wrong.
    """
    remaining = int(seconds * 1000)
    while remaining > 0:
        feed_watchdog()
        step = 500 if remaining > 500 else remaining
        time.sleep_ms(step)
        remaining -= step
    feed_watchdog()

# Send a telegram message to a given user id
def send_message (chatId, message):
    param = {'chat_id': chatId, 'text': message}
    outcome, status, body = do_request('POST', sendURL, param)
    if outcome not in (REQUEST_OK, REQUEST_SKIPPED) and status != 0:
        # status 0 means do_request already reported the transport error.
        report('sendMessage failed: ' + describe_api_error(status, body))
    return (outcome, status, body)

def discard_update_backlog():
    """Drop anything Telegram has been holding for us.

    `updateId` lives in RAM, so every reset restarts it at zero and
    getUpdates replays the backlog -- re-executing commands sent up to 24
    hours ago. Observed: a `/log` answered again after every reboot.

    An offset of -1 asks for the last update only; acknowledging it clears
    everything before it.
    """
    global updateId
    outcome, status, body = do_request('GET', getURL + '?offset=-1&limit=1')
    if outcome != REQUEST_OK:
        return False
    try:
        results = body['result']
    except (KeyError, TypeError):
        return False
    if results:
        updateId = results[-1]['update_id'] + 1
        report('Discarded ' + str(len(results)) + ' stale update(s)')
    return True

def read_message(chatId):
    global updateId
    url = ''
    if (updateId != 0):
        url = getURL + "?offset=" + str(updateId) + "?chat_id=" + str(chatId)
    else:
        url = getURL + "?chat_id=" + str(chatId)
    # NOTE: never print or log `url` -- it embeds the bot token.
    outcome, status, body = do_request('GET', url)
    if outcome == REQUEST_SKIPPED:
        return
    if outcome != REQUEST_OK:
        # status 0 means do_request already reported the transport error
        # with its errno; repeating it as 'status=0' adds nothing.
        if status != 0:
            append_to_log('getUpdates failed: ' +
                          describe_api_error(status, body))
        return
    for result in body['result']:
        updateId = result['update_id'] + 1
        command = result['channel_post']['text']
        if (command == logCommand):
            print_log(chatId)
        elif (command == statusCommand):
            send_message(chatId, heartbeat_text())

def report(message):
    """Log it and say it.

    append_to_log() alone writes to a buffer nobody is watching. Several
    events that only matter while someone is looking -- ring widths,
    rejected transients, the stability write -- were invisible on the
    console because of that.
    """
    print(message)
    append_to_log(message)

def append_to_log(message):
    """Append, dropping the oldest lines once full.

    Previously this refused new entries at the limit, which preserved the
    *least* recent events and printed a full-log notice on every call. A
    long outage therefore filled the log with the start of the outage,
    discarded the errors that explained it, and buried the console in
    notices. Exactly backwards for diagnosis.
    """
    logLines.append(str(time.ticks_ms()) + ' ' + message)
    while len(logLines) > LOG_MAX_LINES:
        # Drop the oldest. Refusing new entries instead, as this once did,
        # preserves the least recent events -- backwards for diagnosis.
        logLines.pop(0)

def log_text():
    return '\n'.join(logLines)

def print_log(chatId):
    """Send the log, in pieces, and clear it only once it has all landed.

    Telegram caps a message at 4096 characters. The log was allowed to
    reach 10000, so a full log was an unconditional 400 -- and the old code
    then cleared it anyway, destroying the thing that had just failed to
    send. Observed: /log worked early on and stopped once the log filled.
    """
    if not logLines:
        send_message(chatId, 'Log is empty.')
        return True
    pending = log_text()
    while pending:
        chunk = pending[:LOG_CHUNK_CHARS]
        if len(pending) > LOG_CHUNK_CHARS:
            edge = chunk.rfind('\n')
            if edge > 0:
                chunk = chunk[:edge]
        outcome, status, body = send_message(chatId, chunk)
        if outcome != REQUEST_OK:
            # Keep everything. A log that failed to send is exactly the log
            # someone needs.
            report('Log send failed; keeping it')
            return False
        pending = pending[len(chunk):]
        if pending[:1] == '\n':
            pending = pending[1:]
    reset_log()
    return True

def reset_log():
    global wifiData
    del logLines[:]
    logLines.append(str(time.ticks_ms()) + ' ' + wifiData)

####################################################################################
# Tier 2 persistence.
#
# Flash endurance is about write *frequency*, not size: a 40 byte file and a
# 4 KB file both cost one 4 KB sector erase. At ~100k cycles per sector, a
# handful of writes per device lifetime is free and one write per minute
# destroys a sector in about 69 days.
#
# THE RULE: never write flash on a timer. Only on a real state transition.
#
# Tier 0 (log, counters, live queue) stays in RAM and is never persisted.
# Tier 1 (reset reason, boot count) belongs in the watchdog scratch registers.
# Tier 3 (queue snapshots) reuses this file but writes only on rare triggers.
####################################################################################

STATE_PATH = 'state.json'
STATE_TMP = 'state.json.tmp'
STATE_VERSION = 3

# Set False when the on-disk file was written by newer firmware, so a
# downgraded build runs on defaults instead of clobbering it.
stateWritable = True

def default_state():
    # Built fresh each call -- a module-level dict literal would be shared
    # and mutated by reference.
    return {
        'v': STATE_VERSION,
        'chatId': None,        # A4: overrides secrets on supergroup migration
        'epochAnchor': None,   # C1: NTP epoch captured at last sync
        'writes': 0,           # I3: wear counter, reported in the heartbeat
        'boots': 0,            # C5: stable boots; survives hardware resets
        'queue': [],           # B6: rings not yet delivered (Tier 3)
    }

def state_is_future(data):
    """True if the file was written by firmware newer than this build."""
    return isinstance(data, dict) and data.get('v', 0) > STATE_VERSION

def migrate_state(data):
    """Upgrade a decoded state file to STATE_VERSION.

    Returns the migrated dict, or None if the shape is unusable. Callers
    treat None as 'no usable state', not as 'do not touch this file' --
    see state_is_future() for that case.
    """
    if not isinstance(data, dict):
        return None
    version = data.get('v', 0)
    # Migrations chain here, oldest first. v1 -> v2 added 'boots' and
    # v2 -> v3 added 'queue'; neither needs a body, because the merge below
    # fills absent keys from defaults. The version bumps still matter, so a
    # downgraded build recognises the file as newer and leaves it alone.
    # Later migrations that do need a body go here:
    #   if version < 4:
    #       data['newField'] = derive_from(data)
    #       version = 4
    merged = default_state()
    for key in merged:
        if key in data:
            merged[key] = data[key]
    merged['v'] = STATE_VERSION
    return merged

def load_state():
    """Read Tier 2 state from flash. Always returns a usable dict."""
    global stateWritable
    # A leftover temp file means we lost power mid-write. The rename never
    # happened, so state.json is still the last good copy; drop the scrap.
    try:
        os.remove(STATE_TMP)
        append_to_log('Discarded stale ' + STATE_TMP)
    except OSError:
        pass

    raw = None
    try:
        f = open(STATE_PATH, 'r')
        try:
            raw = json.load(f)
        finally:
            f.close()
    except OSError:
        # No state file: first boot, or nothing has ever needed persisting.
        return default_state()
    except ValueError:
        append_to_log('State file corrupt, falling back to defaults')
        return default_state()

    if state_is_future(raw):
        # Written by newer firmware. Do not guess at its schema and do not
        # overwrite it -- the user may simply have rolled back.
        stateWritable = False
        append_to_log('State file is newer than firmware; running read-only')
        return default_state()

    migrated = migrate_state(raw)
    if migrated is None:
        # Structurally wrong but syntactically valid, e.g. a bare list.
        # Nothing meaningful to preserve, so stay writable and let the
        # next real transition overwrite it.
        append_to_log('State file unusable, falling back to defaults')
        return default_state()
    return migrated

def save_state():
    """Write Tier 2 state atomically. Returns True on success.

    Writes to a temp file and renames over the target. Rename is atomic
    under littlefs, so a power cut can never leave a half-written state
    file -- it leaves either the old copy or the new one.
    """
    global state
    if not stateWritable:
        return False
    state['writes'] = state.get('writes', 0) + 1
    payload = json.dumps(state)
    f = None
    try:
        f = open(STATE_TMP, 'w')
        f.write(payload)
        f.close()
        f = None
        os.rename(STATE_TMP, STATE_PATH)
        return True
    except OSError as e:
        append_to_log('State save failed: ' + str(e))
        if f is not None:
            try:
                f.close()
            except Exception:
                pass
        try:
            os.remove(STATE_TMP)
        except OSError:
            pass
        return False

def state_get(key, fallback=None):
    return state.get(key, fallback)

def state_set(key, value):
    """Set a Tier 2 value, writing flash only if it actually changed.

    The in-RAM dict mirrors what is on flash, so comparing here is the
    read-before-write check -- an unchanged value costs no erase.
    """
    global state
    if state.get(key, None) == value:
        return False
    state[key] = value
    return save_state()

####################################################################################
# Tier 1 persistence: watchdog scratch registers.
#
# 32 bytes that survive a warm reset but not a power cut, at zero flash cost.
# That asymmetry is the point: if the magic word is still there, power was
# never lost, which is an independent cross-check on machine.reset_cause().
####################################################################################

WATCHDOG_BASE = 0x40058000
WATCHDOG_REASON = WATCHDOG_BASE + 0x08
WATCHDOG_SCRATCH0 = WATCHDOG_BASE + 0x0c   # SCRATCH0..7, four bytes each

# Scratch 4-7 carry the bootrom's reboot-to-BOOTSEL handshake, so stay in
# 0-3. Using the top of that range leaves 0 and 1 alone in case the port
# wants them.
# Set immediately before a reset this firmware chose to perform, so the next
# boot can tell its own doing from a watchdog bite. Both look identical
# otherwise: warm, with no CHIP_RESET flags.
# UNVERIFIED: scratch 0 and 1 are believed unused by the port, but only
# 4-7 are documented as taken by the bootrom.
SCRATCH_REASON_IDX = 0        # why self_reset() fired, for the next boot
REASON_NAMES = {
    0: '',
    1: 'WiFi unreachable',
    2: 'network dead after a bounce',
}
REASON_WIFI = 1
REASON_NETWORK = 2

SCRATCH_INTENT_IDX = 1
SCRATCH_INTENT = 0x50444252   # 'PDBR'

SCRATCH_MAGIC_IDX = 2
# Counts boots since the last one that proved stable. Not a total: any
# hardware reset clears the scratch area, so a running total has to live in
# flash (see 'boots' in state.json). This one exists to make boot loops
# visible, which is precisely the case where flash must not be written.
SCRATCH_UNSTABLE_IDX = 3

# How long the device must stay up before a boot counts as stable.
BOOT_STABLE_MS = 60000

bootNumber = None
bootRecorded = False
bootStableAt = 0
SCRATCH_MAGIC = 0x50444231   # 'PDB1'

# RP2040 VREG_AND_CHIP_RESET.CHIP_RESET. Records what caused the last reset
# in hardware, independently of what MicroPython reports.
# UNVERIFIED: bit positions are from the datasheet but have not been
# confirmed on a board. The raw word is logged as well, so a wrong decode
# here cannot destroy the underlying evidence.
CHIP_RESET = 0x40064000 + 0x08
CHIP_RESET_HAD_POR = 1 << 8          # power-on or brown-out
CHIP_RESET_HAD_RUN = 1 << 16         # RUN pin pulled low
CHIP_RESET_HAD_PSM_RESTART = 1 << 20 # restart from the debug port

resetInfo = None

def scratch_read(index):
    return machine.mem32[WATCHDOG_SCRATCH0 + (index * 4)]

def scratch_write(index, value):
    machine.mem32[WATCHDOG_SCRATCH0 + (index * 4)] = value & 0xFFFFFFFF

def reset_cause_name(value):
    # Which constants exist varies by port and version, so match by lookup
    # rather than assuming any particular one is defined.
    for name in ('PWRON_RESET', 'HARD_RESET', 'WDT_RESET',
                 'DEEPSLEEP_RESET', 'SOFT_RESET'):
        if getattr(machine, name, None) == value:
            return name
    return 'UNKNOWN_' + str(value)

def decode_chip_reset(word):
    flags = []
    if word & CHIP_RESET_HAD_POR:
        flags.append('POR/BOD')
    if word & CHIP_RESET_HAD_RUN:
        flags.append('RUN')
    if word & CHIP_RESET_HAD_PSM_RESTART:
        flags.append('DEBUG')
    return flags

def read_reset_info():
    """Capture why we restarted, and how many times.

    Three independent sources, because no single one is trustworthy on its
    own for the question we are asking:

      - machine.reset_cause(), the port's own interpretation
      - CHIP_RESET, the hardware's record: separates a supply brownout
        (POR/BOD) from the RUN pin being pulled low
      - the scratch magic word, which survives a warm reset but not a
        power cut, so its absence independently confirms power was lost

    Never raises. Diagnostics must not be able to stop the device booting.
    """
    info = {
        'cause': 'unavailable',
        'causeRaw': None,
        'chipReset': None,
        'flags': [],
        'wdtReason': None,
        'unstableBoots': 0,
        'bootNumber': None,
        'selfReset': False,
        'resetReason': '',
        'warmBoot': False,
    }
    try:
        raw = machine.reset_cause()
        info['causeRaw'] = raw
        info['cause'] = reset_cause_name(raw)
    except Exception:
        pass
    try:
        word = machine.mem32[CHIP_RESET]
        info['chipReset'] = word
        info['flags'] = decode_chip_reset(word)
    except Exception:
        pass
    try:
        info['wdtReason'] = machine.mem32[WATCHDOG_REASON]
    except Exception:
        pass
    try:
        if scratch_read(SCRATCH_INTENT_IDX) == SCRATCH_INTENT:
            info['selfReset'] = True
            info['resetReason'] = REASON_NAMES.get(
                scratch_read(SCRATCH_REASON_IDX), '')
            scratch_write(SCRATCH_INTENT_IDX, 0)
            scratch_write(SCRATCH_REASON_IDX, 0)
    except Exception:
        pass
    try:
        if scratch_read(SCRATCH_MAGIC_IDX) == SCRATCH_MAGIC:
            # Magic intact: nothing reset the chip, so this was a soft
            # reboot or a watchdog bite.
            info['warmBoot'] = True
            info['unstableBoots'] = scratch_read(SCRATCH_UNSTABLE_IDX) + 1
        else:
            info['unstableBoots'] = 1
            scratch_write(SCRATCH_MAGIC_IDX, SCRATCH_MAGIC)
        scratch_write(SCRATCH_UNSTABLE_IDX, info['unstableBoots'])
    except Exception:
        pass
    return info

def reset_verdict(info):
    """Decide what actually caused the reset.

    Deliberately ignores machine.reset_cause() and WATCHDOG_REASON. Both
    are unreliable on rp2: the bootrom uses the watchdog to launch the
    application, so REASON carries the TIMER bit through ordinary
    startup. Observed on hardware, same board, minutes apart:
    cause=WDT_RESET on a soft reboot, then cause=PWRON_RESET on a RUN-pin
    reset. Neither was right.

    CHIP_RESET and the scratch magic are trustworthy, and were both
    confirmed on hardware: POR reads 0x00000100, RUN reads 0x00010000,
    and the bits do not accumulate -- each reset reports only its own
    cause.

    The scratch area is cleared by any hardware reset, RUN included, not
    only by power loss. So a surviving magic word means no hardware reset
    occurred, which in turn means CHIP_RESET still describes some earlier
    event and must be ignored.
    """
    if info is None:
        return 'unknown'
    if info['selfReset']:
        # We asked for this one. Without the marker it would be
        # indistinguishable from a watchdog bite.
        return 'self-reset'
    if info['warmBoot']:
        # Scratch survived, so nothing reset the chip: a soft reboot or a
        # watchdog bite. CHIP_RESET here is stale.
        #
        # WATCHDOG_REASON separates them. Bit 0 is TIMER, an actual timeout;
        # bit 1 is FORCE, which machine.reset() uses. Observed on the bench:
        # wdt=0x1 for a genuine bite during a slow request, wdt=0x2 for a
        # deliberate reset. Only trusted on a warm boot -- the bootrom sets
        # TIMER during an ordinary cold start.
        reason = info['wdtReason'] or 0
        if reason & 0x1:
            return 'watchdog'
        return 'warm-reset'
    flags = info['flags']
    if 'POR/BOD' in flags:
        # Supply dropped, or this is the first power-up.
        return 'power'
    if 'RUN' in flags:
        return 'run-pin'
    return 'unknown'

def format_reset_info(info):
    if info is None:
        return 'Reset info unavailable'
    number = info['bootNumber']
    parts = ['boot #' + (str(number) if number is not None else '?'),
             'verdict=' + reset_verdict(info)]
    if info['unstableBoots'] > 1:
        # More than one attempt since the last stable boot: a loop.
        parts.append('unstable=' + str(info['unstableBoots']))
    if info['flags']:
        # Stale on a warm boot -- see reset_verdict().
        label = 'chip' if not info['warmBoot'] else 'chip(stale)'
        parts.append(label + '=' + '+'.join(info['flags']))
    if info['chipReset'] is not None:
        parts.append('raw=0x%08x' % info['chipReset'])
    if info['resetReason']:
        parts.append('reason=' + info['resetReason'].replace(' ', '_'))
    parts.append('warm' if info['warmBoot'] else 'cold')
    # Advisory only. Kept because it is free and occasionally corroborates.
    advisory = 'cause=' + str(info['cause'])
    if info['wdtReason']:
        advisory += ' wdt=0x%x' % info['wdtReason']
    parts.append('(' + advisory + ')')
    return 'Reset: ' + ' '.join(parts)

# Populated for real by boot(). Defined here so state_get/state_set always
# have a dict to work with, without touching flash at import time.
state = default_state()


# Define blinking function for onboard LED to indicate error codes    
def blink_onboard_led(num_blinks):
    for i in range(num_blinks):
        led.on()
        time.sleep(.2)
        led.off()
        time.sleep(.2)
        
def is_wifi_connected():
    wlan_status = wlan.status()
    if wlan_status != 3:
        return False
    else:
        return True

def wifi_status_name(code):
    return WIFI_STATUS_NAMES.get(code, 'status ' + str(code))

def self_reset(reason, code=0):
    """Reset deliberately, leaving a marker so the next boot knows.

    The RAM log does not survive, so a short code goes into scratch 0. It
    is the one fact worth carrying across: without it, a reset loop caused
    by an AP that keeps rejecting the board looks like any other
    self-reset, and the explanation is destroyed every few minutes by the
    very reset it caused.
    """
    report('Resetting: ' + reason)
    try:
        scratch_write(SCRATCH_REASON_IDX, code)
    except Exception:
        pass
    try:
        snapshot_queue()          # do not lose undelivered rings
    except Exception:
        pass
    try:
        scratch_write(SCRATCH_INTENT_IDX, SCRATCH_INTENT)
    except Exception:
        pass
    time.sleep(1)                 # let the console drain
    machine.reset()

def connect_wifi():
    """Bring the WiFi link up. Connection only -- no notifications.

    Callers decide whether to announce; mixing the two made a transport
    failure look like a connection failure.

    One connect() is issued and then given WIFI_REISSUE_MS to work before
    another is tried. The previous code re-issued every three seconds,
    which left the stack associated but unable to pass traffic after a long
    outage. After WIFI_MAX_ATTEMPTS the board resets, because at that point
    a wedged driver is the likeliest remaining explanation and a reset is
    the only thing that clears it.
    """
    global wifiData, lastNetworkSuccess
    attempts = 0
    issuedAt = None
    while True:
        if (is_wifi_connected()):
            blink_onboard_led(3)
            led.on()
            status = wlan.ifconfig()
            print('ip = ' + status[0])
            wifiData = 'WiFi connected. IP: ' + status[0]
            append_to_log(wifiData)
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
            reissue = time.ticks_diff(now, issuedAt) > WIFI_PROGRESS_MAX_MS
        else:
            reissue = time.ticks_diff(now, issuedAt) > WIFI_REISSUE_MS

        if reissue:
            attempts += 1
            if attempts > WIFI_MAX_ATTEMPTS:
                self_reset('WiFi unreachable after ' + str(attempts - 1) +
                           ' attempts', REASON_WIFI)
            led.off()
            report('WiFi down (' + wifi_status_name(status) +
                   '), attempt ' + str(attempts))
            try:
                wlan.connect(ssid, pw)
            except OSError as e:
                report('WiFi connect failed: ' + str(e))
            issuedAt = now

        sleep_fed(WIFI_POLL_MS / 1000.0)
        # The loop is blocked here for as long as the outage lasts, so the
        # queue would otherwise never reach flash during the one situation
        # it exists for.
        maybe_snapshot_queue()

def announce_startup():
    """Tell the chat we are up. Best effort -- never fatal.

    Sending this used to live inside connect_wifi(), where a failure --
    typically DNS not yet ready straight after association -- killed the
    boot before the doorbell input was ever configured.
    """
    global isStartup, pendingAnnouncement
    if (isStartup):
        # Carry the reset diagnosis into the chat. The reboots being chased
        # happen on the production unit, not the bench, so this message is
        # the only place the evidence reliably surfaces.
        pendingAnnouncement = startupText + '\n' + format_reset_info(resetInfo)
        isStartup = False
    else:
        # Deliberately no reset summary. Nothing reset -- the network came
        # back. Repeating the last boot's diagnosis here made a WiFi blip
        # look like a reboot, which corrupts the very dataset C4 collects.
        pendingAnnouncement = reconnectText
    return flush_announcement()

def flush_announcement():
    """Deliver the pending announcement, retrying until it lands.

    Sent directly rather than through the ring queue, but with the same
    rule: it is not discarded until Telegram confirms it. The startup
    message carries the reset diagnosis, and losing that to a momentary
    outage would quietly cost the reboot investigation its data.
    """
    global pendingAnnouncement
    if pendingAnnouncement is None:
        return True
    outcome, status, body = send_message(chatId, pendingAnnouncement)
    if outcome == REQUEST_OK:
        pendingAnnouncement = None
        return True
    if outcome == REQUEST_FATAL:
        report('Announcement undeliverable; dropping it')
        pendingAnnouncement = None
    return False

def make_edge_handler(entry):
    """Build the interrupt handler for one input.

    The closure is created once at setup; calling it allocates nothing,
    which is the requirement for a MicroPython ISR. It does no I/O, no
    string work and no logging -- it stamps two integers and returns.

    Both edges are watched deliberately. Capturing the falling edge in
    hardware means the pulse width is known exactly, even if the main loop
    was blocked in a TLS handshake for several seconds and only gets to
    look afterwards. Validating by re-reading the pin instead would have
    failed in precisely that case: the pulse would be long over, the pin
    back at ground, and a real ring discarded as noise.
    """
    def handler(pin):
        now = time.ticks_ms()
        if pin.value() == pressed:
            if (entry[IN_COMPLETE] and
                    time.ticks_diff(now, entry[IN_FALL]) < DEBOUNCE_MS):
                # The line came back up almost immediately, so the fall was
                # contact chatter on the way in, not the release. Reopen the
                # pulse and keep the original rise time.
                entry[IN_COMPLETE] = False
                entry[IN_LAST_EDGE] = now
                return
            if entry[IN_PENDING] and not entry[IN_COMPLETE]:
                # Already mid-pulse; nothing to do but note the edge.
                entry[IN_LAST_EDGE] = now
                return
            entry[IN_LAST_EDGE] = now
            entry[IN_RISE] = now
            entry[IN_COMPLETE] = False
            entry[IN_PENDING] = True
        else:
            entry[IN_LAST_EDGE] = now
            if entry[IN_PENDING] and not entry[IN_COMPLETE]:
                # Provisional. process_input() waits a debounce window
                # before trusting it, and a rise inside that window undoes
                # it.
                entry[IN_FALL] = now
                entry[IN_COMPLETE] = True
    return handler

def add_input(name, pinNumber):
    """Register a latched input and arm its interrupt."""
    pin = machine.Pin(pinNumber, machine.Pin.IN, machine.Pin.PULL_DOWN)
    entry = [0] * IN_FIELDS
    entry[IN_NAME] = name
    entry[IN_PIN] = pin
    entry[IN_PENDING] = False
    entry[IN_COMPLETE] = False
    inputs.append(entry)
    pin.irq(handler=make_edge_handler(entry),
            trigger=machine.Pin.IRQ_RISING | machine.Pin.IRQ_FALLING)
    print(name + ' input ready on GP' + str(pinNumber))
    return entry

def was_unpollable(entry):
    """Would the old polling loop have missed this ring?

    True when the whole pulse fell between two passes of the main loop, so
    no poll could have observed it. Counted rather than acted upon: it
    turns 'we might have been dropping rings' into a number.
    """
    if not entry[IN_COMPLETE]:
        return False
    return (time.ticks_diff(entry[IN_RISE], prevPassTicks) > 0 and
            time.ticks_diff(lastPassTicks, entry[IN_FALL]) > 0)

def process_input(entry):
    """Decide what a latched edge was, and act on it."""
    now = time.ticks_ms()
    width = None
    if entry[IN_COMPLETE]:
        if time.ticks_diff(now, entry[IN_FALL]) < DEBOUNCE_MS:
            # The close is still provisional: the line may yet bounce back
            # up and prove this was chatter rather than the release.
            return
        width = time.ticks_diff(entry[IN_FALL], entry[IN_RISE])
    elif time.ticks_diff(now, entry[IN_RISE]) > STUCK_INPUT_MS:
        # Still high long after any real ring would have ended.
        entry[IN_PENDING] = False
        report(entry[IN_NAME] + ' input stuck high')
        return
    else:
        # Mid-pulse. Leave it latched and look again next pass.
        return

    entry[IN_PENDING] = False

    if width < MIN_PULSE_MS:
        entry[IN_REJECTED] += 1
        report(entry[IN_NAME] + ' transient ignored, ' + str(width) + 'ms')
        return

    if was_unpollable(entry):
        entry[IN_MISSED] += 1

    if (entry[IN_RINGS] > 0 and
            time.ticks_diff(now, entry[IN_LAST_ALERT]) < ALERT_LOCKOUT_MS):
        # Same ring, or an impatient second press. One alert is enough.
        return

    entry[IN_RINGS] += 1
    entry[IN_LAST_ALERT] = now
    report(entry[IN_NAME] + ' ring, ' + str(width) + 'ms')
    # Queued rather than sent. Delivery is the queue's job, and a ring is
    # not discarded until Telegram confirms it.
    enqueue_ring(width)

def current_epoch():
    """Wall-clock seconds, or None until C1 syncs the clock.

    Until then a queued ring has no knowable absolute time, and one
    restored from flash cannot even be aged -- ticks_ms restarts at zero.
    The field exists now so C1 needs no schema change.
    """
    return state_get('epochAnchor', None)

def enqueue_ring(width):
    global queueDropped
    if len(queue) >= QUEUE_MAX:
        # Drop the oldest: a visitor from an hour ago matters less than the
        # one at the door now.
        queue.pop(0)
        queueDropped += 1
        append_to_log('Queue full, dropped the oldest ring')
    queue.append([time.ticks_ms(), width, current_epoch(), False])
    if networkFailures:
        # The network is already failing, so this ring may sit here a while
        # -- and a watchdog bite during a slow DNS lookup would take it with
        # it. One write, only for rings that arrive during trouble.
        snapshot_queue()

def describe_delay(entry):
    if entry[Q_RESTORED]:
        return ' (queued before a restart)'
    age = time.ticks_diff(time.ticks_ms(), entry[Q_TICKS])
    if age < DELAY_NOTICE_MS:
        return ''
    return ' (delayed ' + str(age // 1000) + 's)'

def flush_queue():
    """Deliver what is waiting. Stops at the first retryable failure.

    Nothing leaves the queue until Telegram has confirmed it. The previous
    code sent and forgot, so a failed send lost the ring silently -- seen
    on the bench as a logged ring that never arrived.
    """
    sentCount = 0
    while queue and sentCount < QUEUE_FLUSH_PER_PASS:
        entry = queue[0]
        outcome, status, body = send_message(chatId, text + describe_delay(entry))
        if outcome == REQUEST_OK:
            queue.pop(0)
            sentCount += 1
        elif outcome == REQUEST_FATAL:
            # Retrying will not help. Drop it rather than block the queue
            # behind something permanently undeliverable.
            queue.pop(0)
            report('Dropped an undeliverable ring: ' +
                   describe_api_error(status, body))
        else:
            # Transient or rate limited. Leave it and try again next pass.
            break
    return sentCount

def snapshot_queue():
    """Persist the queue. Writes only if the contents actually changed."""
    global lastSnapshotTicks
    lastSnapshotTicks = time.ticks_ms()
    state_set('queue', [[e[Q_EPOCH], e[Q_WIDTH]] for e in queue])

def maybe_snapshot_queue():
    """Persist a queue that has been waiting long enough to be at risk.

    Not on a timer. A short outage never writes at all, because RAM already
    covers it. Only an outage that has already run for minutes -- long
    enough that a reset in the middle is a real possibility -- earns a
    flash write.
    """
    now = time.ticks_ms()
    if not queue:
        if state_get('queue', []):
            snapshot_queue()      # delivered; clear the copy on flash
        return
    if time.ticks_diff(now, queue[0][Q_TICKS]) < QUEUE_SNAPSHOT_AFTER_MS:
        return
    if time.ticks_diff(now, lastSnapshotTicks) < QUEUE_SNAPSHOT_MIN_MS:
        return
    snapshot_queue()

def restore_queue():
    """Reload rings that outlived a reset."""
    stored = state_get('queue', [])
    if not stored:
        return
    now = time.ticks_ms()
    for item in stored:
        try:
            epoch = item[0]
            width = item[1]
        except (IndexError, TypeError):
            continue
        queue.append([now, width, epoch, True])
    report('Recovered ' + str(len(queue)) + ' undelivered ring(s)')

def poll_inputs():
    for entry in inputs:
        if entry[IN_PENDING]:
            process_input(entry)

def format_uptime(ms):
    seconds = ms // 1000
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    if days:
        return str(days) + 'd ' + str(hours) + 'h ' + str(minutes) + 'm'
    if hours:
        return str(hours) + 'h ' + str(minutes) + 'm'
    return str(minutes) + 'm'

def wifi_rssi():
    try:
        return str(wlan.status('rssi')) + ' dBm'
    except Exception:
        return 'unknown'

def heartbeat_text():
    """Everything worth knowing about a device nobody is watching.

    Free memory leads because it is the one figure that only uptime can
    produce: the socket-leak fix in do_request cannot be proven by any
    single reading, and getting one by hand means killing the run to reach
    a REPL -- which the watchdog then resets out from under you.
    """
    gc.collect()
    lines = [
        'Still here.',
        'Uptime: ' + format_uptime(uptimeMs),
        'Boot: #' + str(bootNumber if bootNumber is not None else '?') +
        ' (' + reset_verdict(resetInfo) + ')',
        'Free memory: ' + str(gc.mem_free()) + ' bytes',
        'WiFi: ' + wifi_rssi() + ', ' + str(wifiData),
        input_summary(),
        'Flash writes: ' + str(state_get('writes', 0)),
    ]
    if networkFailures:
        lines.append('Network: ' + str(networkFailures) +
                     ' consecutive failure(s)')
    if pendingAnnouncement is not None:
        lines.append('An announcement is still undelivered.')
    return '\n'.join(lines)

def maybe_heartbeat():
    """Report in, on schedule.

    Routed through pendingAnnouncement so a failed heartbeat is retried
    rather than lost -- a missing "still alive" message is exactly what a
    dead device looks like. Skipped if that slot is occupied: a startup
    report matters more, and the next heartbeat is only hours away.
    """
    global lastHeartbeat, pendingAnnouncement
    if time.ticks_diff(time.ticks_ms(), lastHeartbeat) < HEARTBEAT_MS:
        return False
    lastHeartbeat = time.ticks_ms()
    if pendingAnnouncement is not None:
        return False
    pendingAnnouncement = heartbeat_text()
    return True

def input_summary():
    """One line per input, for the heartbeat (C3)."""
    parts = []
    for entry in inputs:
        parts.append(entry[IN_NAME] + ': ' + str(entry[IN_RINGS]) +
                     ' rings, ' + str(entry[IN_REJECTED]) +
                     ' transients, ' + str(entry[IN_MISSED]) +
                     ' unpollable')
    parts.append('queue: ' + str(len(queue)) + ' waiting, ' +
                 str(queueDropped) + ' dropped')
    return '; '.join(parts)

def setup_hardware():
    """Configure the pins. Must happen before anything network-related.

    wlan.active(True) lives here because on the Pico W the onboard LED
    hangs off the CYW43 chip -- machine.Pin('LED') is unusable until the
    wireless interface is powered up.
    """
    global led, doorBellInput, mac
    wlan.active(True)
    # Before connecting: the setting applies to the association.
    mode = WIFI_PM_PERFORMANCE if wifiPowerSave else WIFI_PM_NONE
    try:
        wlan.config(pm=mode)
        print('WiFi power save ' + ('on' if wifiPowerSave else 'off'))
    except Exception as e:
        # Not fatal. An unconfigurable radio still answers the door.
        print('Could not set WiFi power mode: ' + str(e))
    led = machine.Pin('LED', machine.Pin.OUT)
    doorBellInput = add_input('Doorbell', doorBellPin)[IN_PIN]
    # MAC lives in the wireless chip OTP. Read it from the interface we
    # already have rather than constructing a second WLAN object.
    mac = ubinascii.hexlify(wlan.config('mac'), ':').decode()
    print('mac = ' + mac)

def error_halt(message):
    """Signal an unrecoverable setup fault on the LED.

    Reached only when the board cannot be configured at all, which in
    practice means a bad pin number. A human has to fix it, so blink
    rather than reset. B2 will let the watchdog escalate this.
    """
    print('FATAL: ' + message)
    # boot() normally logs this after state loads; on this path it never
    # gets there, and the reset cause is the thing worth having.
    print(format_reset_info(resetInfo))
    while True:
        try:
            if led is not None:
                led.on()
                time.sleep(.08)
                led.off()
                time.sleep(.08)
            else:
                time.sleep(1)
        except Exception:
            time.sleep(1)

def mark_boot_stable():
    """Record this boot in flash, once it has proven it can stay up.

    Called from the main loop, but this is not a timed write: it fires at
    most once per boot, and during a boot loop it never fires at all --
    which is the whole point. A device resetting every five seconds would
    otherwise manage 17,000 writes a day and kill a sector inside a week.

    The number therefore counts *stable* boots. Any divergence between it
    and reality is itself informative: the Tier 1 unstable counter carries
    the attempts that did not get this far.
    """
    global bootRecorded
    if bootRecorded or bootNumber is None:
        return
    if time.ticks_diff(time.ticks_ms(), bootStableAt) < 0:
        return
    bootRecorded = True
    state_set('boots', bootNumber)
    # A sector erase stalls the CPU for tens of milliseconds, occasionally
    # more, and runs with interrupts disabled.
    feed_watchdog()
    # Attempts since the last stable boot are now history.
    try:
        scratch_write(SCRATCH_UNSTABLE_IDX, 0)
    except Exception:
        pass
    # The only flash write in normal operation; a silent one is hard to
    # confirm while validating.
    report('Boot ' + str(bootNumber) + ' stable after ' +
           str(BOOT_STABLE_MS // 1000) + 's')

def boot():
    """Bring the device up, hardware first.

    Ordering is the whole point. Previously connect_wifi() ran at module
    scope, outside any try, and sent the startup message immediately after
    association -- exactly when DNS is least likely to be ready. If that
    send raised, the script died before machine.Pin(16) was ever reached
    and the doorbell was dead until someone power-cycled it.

    Now: pins, then flash, then network. Only the first is fatal.
    """
    global state, resetInfo, bootNumber, bootStableAt, lastHeartbeat
    # First, before anything can fail. The scratch registers are volatile
    # and a later crash would take the evidence with it.
    resetInfo = read_reset_info()
    lastHeartbeat = time.ticks_ms()

    try:
        setup_hardware()
    except Exception as e:
        # No usable input pin means there is nothing to do.
        error_halt('could not configure hardware: ' + str(e))

    try:
        state = load_state()
    except Exception as e:
        state = default_state()
        append_to_log('State load failed, using defaults: ' + str(e))
        print('State load failed, using defaults: ' + str(e))

    # The running total lives in flash, so it is only knowable once state
    # has loaded -- which is why the summary is logged here rather than at
    # the top of boot(). Nothing is written yet; see mark_boot_stable().
    restore_queue()

    bootNumber = state_get('boots', 0) + 1
    resetInfo['bootNumber'] = bootNumber
    bootStableAt = time.ticks_add(time.ticks_ms(), BOOT_STABLE_MS)

    summary = format_reset_info(resetInfo)
    print(summary)
    append_to_log(summary)

    # Everything that could legitimately hang from here on is network work.
    arm_watchdog()

    try:
        connect_wifi()
        # Before announcing: otherwise a reset replays every command
        # Telegram has been holding, including ones from yesterday.
        discard_update_backlog()
        announce_startup()
    except Exception as e:
        # Network trouble is the main loop's problem, not a boot failure.
        append_to_log('Startup networking failed: ' + str(e))
        print('Startup networking failed: ' + str(e))

boot()

while True:
    try:
        if (not is_wifi_connected()):
            connect_wifi()
            announce_startup()
        
        if networkBounceRequested:
            bounce_wifi()

        poll_inputs()
        flush_announcement()
        flush_queue()
        maybe_snapshot_queue()
        
        # Check for new messages
        if (time.ticks_diff(time.ticks_ms(), lastLogCheck) > logCheckInterval):
            # Log only. Printed once a minute it was pure noise, and it
            # crowded out the events worth seeing in a long run.
            append_to_log('Checking for new messages')
            read_message(chatId)
            lastLogCheck = time.ticks_ms()
        
        mark_boot_stable()
        feed_watchdog()

        # Record when this pass ran, so was_unpollable() can tell whether a
        # ring landed in a gap the old polling loop could not have covered.
        prevPassTicks = lastPassTicks
        lastPassTicks = time.ticks_ms()
        uptimeMs += time.ticks_diff(lastPassTicks, prevPassTicks)
        maybe_heartbeat()

        sleep_fed(loopDelay)
        
    
    except KeyboardInterrupt:
        print('KeyboardInterrupt')
        if wdt is not None:
            # Nothing can disarm an RP2040 watchdog. Leaving the loop stops
            # the feeding, so the board resets shortly. That is correct in
            # service -- an exited loop is a dead doorbell -- but it is worth
            # saying out loud on the bench.
            print('Watchdog is armed: expect a reset within ' +
                  str(WDT_TIMEOUT_MS // 1000) + 's')
        break
    except Exception as e:
        print(e)
        led.off()
        wlan.disconnect()
        append_to_log('WiFi disconnected: ' + str(e))
        # Grace period, in fed slices. Left as a single sleep(10) this
        # would outlast the watchdog and reset the board on every error.
        sleep_fed(10)
        led.on()
        pass
