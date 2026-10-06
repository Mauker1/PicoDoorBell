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

import ubinascii
import machine
import time
import gc
import config
import wdt
import clockmod
import led
import applog
import persist
import resets
import net
import timesync
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

# C1: seconds to add to UTC for displayed timestamps. Defaults to 0, so the
# device shows and logs UTC unless a board.py opts into an offset. UTC is the
# deliberate default: NTP only ever yields UTC, the conversion to local time
# is the client's job, and a fixed offset here cannot follow daylight saving,
# so it would be silently an hour wrong for months of the year. On a resource
# constrained part with no timezone database, and for an open project run in
# many zones, one unambiguous clock everywhere beats a local one that drifts.
# The field remains an escape hatch for a fixed-offset install that wants it;
# every timestamp is tagged with the offset it used (for example +0000) so any
# such choice stays visible rather than silent.
utcOffset = getattr(board, 'utcOffset', 0)
applog.utcOffset = utcOffset
timesync.utcOffset = utcOffset

# Configured by setup_hardware() during boot, not at import time, so that
# failures are catchable and the ordering is explicit. The LED pin lives in
# the led module (led.init), not here.
doorBellInput = None
mac = ''

# Load login data from different file for safety reasons
# WiFi credentials are handed to net in the wiring block before boot().
botToken = secrets['botToken']
chatId = secrets['telegramDmUid']

# Messages and commands now live in config.py.

# The in-memory log lives in applog.py.

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

lastHeartbeat = 0
# Accumulated rather than derived from ticks_ms, which wraps at ~12.4 days and
# is ambiguous past half of that. Summing per-pass differences is wrap-safe
# for as long as the device runs.
uptimeMs = 0

# Time variables
startupTime = time.ticks_ms()
lastLogCheck = startupTime

####################################################################################
# C1 wall clock.
#
# ticks_ms answers "how long since boot", never "what time is it", and it
# wraps at ~12.4 days. A log line reading 3847221 tells an incident report
# nothing. NTP gives real time, but polling it per event would be absurd, so
# one sync captures an anchor: the epoch at a known ticks_ms, from which any
# later wall-clock time is anchor + elapsed. ticks_diff makes the elapsed part
# wrap-safe, so the derived clock outlives the ticks_ms wrap the raw counter
# cannot.
#
# The anchor lives in RAM and is authoritative for "is the clock live". The
# copy in state.json (epochAnchor) is a fallback for aging rings recovered
# after a reset, and is coarse by nature: the device may have been unpowered
# for hours between the anchor being written and the next boot reading it, so
# a restored ring's time is always tagged as approximate. This matches the
# existing honesty about restored rings not being ageable.
#
# NTP sync is never fatal, exactly like announce_startup(): a device with no
# clock still answers the door, and every timestamp simply stays marked as
# relative until a sync lands.
####################################################################################

# NTP timing and the epoch sanity floor now live in config.py.

# The wall-clock anchor and its pure reads live in clockmod.py; the NTP sync
# side (sync_clock, maybe_resync_clock, lastNtpSync) lives in timesync.py.

####################################################################################
# B2 watchdog.
#
# The RP2040 watchdog cannot exceed roughly 8.3 s, which is uncomfortably close to
# what one loop pass can legitimately take. Measured on hardware: a Telegram round
# trip is 1-2 s, and a worst-case pass can hold a send, a getUpdates and a flash
# sector erase.
#
# The margin is therefore bought by feeding from inside the blocking work rather
# than only at the top of the loop -- see wdt.feed_watchdog() call sites. B1 was a
# prerequisite: with its 5 s post-press sleep still in place, a press followed by
# a getUpdates could pass nine seconds without a feed.
#
# Once armed, an RP2040 watchdog cannot be disarmed.
####################################################################################

####################################################################################
# B1 input timings now live in config.py.
####################################################################################

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

# Queue sizing and snapshot timings now live in config.py.

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
    

# The network layer (WiFi, do_request, backoff, wedged-stack detection) lives
# in net.py. These two read Telegram's JSON, not HTTP, so they stay with the
# Telegram code.

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


# The watchdog primitive (arm_watchdog, feed_watchdog, sleep_fed) now lives
# in wdt.py.

# Send a telegram message to a given user id
def send_message (chatId, message):
    param = {'chat_id': chatId, 'text': message}
    outcome, status, body = net.do_request('POST', sendURL, param)
    if outcome not in (net.REQUEST_OK, net.REQUEST_SKIPPED) and status != 0:
        # status 0 means do_request already reported the transport error.
        applog.report('sendMessage failed: ' + describe_api_error(status, body))
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
    outcome, status, body = net.do_request('GET', getURL + '?offset=-1&limit=1')
    if outcome != net.REQUEST_OK:
        return False
    try:
        results = body['result']
    except (KeyError, TypeError):
        return False
    if results:
        updateId = results[-1]['update_id'] + 1
        applog.report('Discarded ' + str(len(results)) + ' stale update(s)')
    return True

def read_message(chatId):
    global updateId
    url = ''
    if (updateId != 0):
        url = getURL + "?offset=" + str(updateId) + "?chat_id=" + str(chatId)
    else:
        url = getURL + "?chat_id=" + str(chatId)
    # NOTE: never print or log `url` -- it embeds the bot token.
    outcome, status, body = net.do_request('GET', url)
    if outcome == net.REQUEST_SKIPPED:
        return
    if outcome != net.REQUEST_OK:
        # status 0 means do_request already reported the transport error
        # with its errno; repeating it as 'status=0' adds nothing.
        if status != 0:
            applog.append_to_log('getUpdates failed: ' +
                          describe_api_error(status, body))
        return
    for result in body['result']:
        updateId = result['update_id'] + 1
        command = result['channel_post']['text']
        if (command == config.logCommand):
            print_log(chatId)
        elif (command == config.statusCommand):
            send_message(chatId, heartbeat_text())

def print_log(chatId):
    """Send the log, in pieces, and clear it only once it has all landed.

    Telegram caps a message at 4096 characters. The log was allowed to
    reach 10000, so a full log was an unconditional 400 -- and the old code
    then cleared it anyway, destroying the thing that had just failed to
    send. Observed: /log worked early on and stopped once the log filled.

    Stays with the network side: applog holds the buffer and its bound,
    while sending it and deciding when to clear it are this caller's
    policy. The seed line for the cleared log is the WiFi status.
    """
    if not applog.logLines:
        send_message(chatId, 'Log is empty.')
        return True
    pending = applog.log_text()
    while pending:
        chunk = pending[:config.LOG_CHUNK_CHARS]
        if len(pending) > config.LOG_CHUNK_CHARS:
            edge = chunk.rfind('\n')
            if edge > 0:
                chunk = chunk[:edge]
        outcome, status, body = send_message(chatId, chunk)
        if outcome != net.REQUEST_OK:
            # Keep everything. A log that failed to send is exactly the log
            # someone needs.
            applog.report('Log send failed; keeping it')
            return False
        pending = pending[len(chunk):]
        if pending[:1] == '\n':
            pending = pending[1:]
    applog.reset_log(net.wifiData)
    return True

# Tier 2 persistence (state.json) lives in persist.py.

####################################################################################
# Boot accounting.
#
# The reset diagnosis (scratch registers, CHIP_RESET, verdicts) lives in
# resets.py. What stays here is the policy that joins it to persist: the
# boot number, and when a boot has stayed up long enough to be recorded.
####################################################################################

bootNumber = None
bootRecorded = False
bootStableAt = 0
resetInfo = None

####################################################################################
# G1 LED state machine.
####################################################################################
# G1 LED state machine now lives in led.py.
####################################################################################

def self_reset(reason, code=0):
    """Reset deliberately, leaving a marker so the next boot knows.

    The RAM log does not survive, so a short code goes into scratch 0. It
    is the one fact worth carrying across: without it, a reset loop caused
    by an AP that keeps rejecting the board looks like any other
    self-reset, and the explanation is destroyed every few minutes by the
    very reset it caused.
    """
    applog.report('Resetting: ' + reason)
    try:
        snapshot_queue()          # do not lose undelivered rings
    except Exception:
        pass
    resets.mark_intended_reset(code)
    time.sleep(1)                 # let the console drain
    machine.reset()

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
        pendingAnnouncement = config.startupText + '\n' + resets.format_reset_info(resetInfo)
        isStartup = False
    else:
        # Deliberately no reset summary. Nothing reset -- the network came
        # back. Repeating the last boot's diagnosis here made a WiFi blip
        # look like a reboot, which corrupts the very dataset C4 collects.
        pendingAnnouncement = config.reconnectText
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
    if outcome == net.REQUEST_OK:
        pendingAnnouncement = None
        return True
    if outcome == net.REQUEST_FATAL:
        applog.report('Announcement undeliverable; dropping it')
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
        if pin.value() == config.pressed:
            if (entry[IN_COMPLETE] and
                    time.ticks_diff(now, entry[IN_FALL]) < config.DEBOUNCE_MS):
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
        if time.ticks_diff(now, entry[IN_FALL]) < config.DEBOUNCE_MS:
            # The close is still provisional: the line may yet bounce back
            # up and prove this was chatter rather than the release.
            return
        width = time.ticks_diff(entry[IN_FALL], entry[IN_RISE])
    elif time.ticks_diff(now, entry[IN_RISE]) > config.STUCK_INPUT_MS:
        # Still high long after any real ring would have ended.
        entry[IN_PENDING] = False
        applog.report(entry[IN_NAME] + ' input stuck high')
        return
    else:
        # Mid-pulse. Leave it latched and look again next pass.
        return

    entry[IN_PENDING] = False

    if width < config.MIN_PULSE_MS:
        entry[IN_REJECTED] += 1
        applog.report(entry[IN_NAME] + ' transient ignored, ' + str(width) + 'ms')
        return

    if was_unpollable(entry):
        entry[IN_MISSED] += 1

    if (entry[IN_RINGS] > 0 and
            time.ticks_diff(now, entry[IN_LAST_ALERT]) < config.ALERT_LOCKOUT_MS):
        # Same ring, or an impatient second press. One alert is enough.
        return

    entry[IN_RINGS] += 1
    entry[IN_LAST_ALERT] = now
    applog.report(entry[IN_NAME] + ' ring, ' + str(width) + 'ms')
    # Queued rather than sent. Delivery is the queue's job, and a ring is
    # not discarded until Telegram confirms it.
    enqueue_ring(width)

def clock_status():
    """One-line clock state for the heartbeat.

    Distinguishes never-synced from synced, and shows how long since the
    last successful sync so drift is visible: a clock that stopped syncing
    hours ago is presenting an increasingly wrong time, and the heartbeat
    is where that should show.
    """
    if not clockmod.clock_is_live():
        return 'unsynced (using relative time)'
    since = time.ticks_diff(time.ticks_ms(), timesync.lastNtpSync)
    return clockmod.format_timestamp(clockmod.clock_now(), utcOffset) + \
        ' (synced ' + format_uptime(since) + ' ago)'

def current_epoch():
    """Wall-clock epoch for a ring arriving now, or None if unsynced.

    A queued ring records the real time it arrived when the clock is live,
    and None when it is not: a ring caught before the first sync still has
    no knowable absolute time, and one restored from flash cannot be aged.
    """
    return clockmod.clock_now()

def enqueue_ring(width):
    global queueDropped
    if len(queue) >= config.QUEUE_MAX:
        # Drop the oldest: a visitor from an hour ago matters less than the
        # one at the door now.
        queue.pop(0)
        queueDropped += 1
        applog.append_to_log('Queue full, dropped the oldest ring')
    queue.append([time.ticks_ms(), width, current_epoch(), False])
    if net.networkFailures:
        # The network is already failing, so this ring may sit here a while
        # -- and a watchdog bite during a slow DNS lookup would take it with
        # it. One write, only for rings that arrive during trouble.
        snapshot_queue()

def describe_delay(entry):
    """Trailing note on a delivered ring: when it rang, or how late it is.

    A ring carrying a real epoch shows its wall-clock time, which is what
    C1 exists for. A restored ring's epoch is only coarse (the device may
    have been off between the anchor write and the next boot), so it is
    tagged approximate. Without any epoch the old relative wording stands:
    a restart note, or a delay in seconds once past the notice threshold.
    """
    epoch = entry[Q_EPOCH]
    if entry[Q_RESTORED]:
        if epoch is not None:
            return ' (rang around ' + \
                   clockmod.format_timestamp(epoch, utcOffset) + \
                   ', before a restart)'
        return ' (queued before a restart)'
    if epoch is not None:
        return ' (' + clockmod.format_timestamp(epoch, utcOffset) + ')'
    age = time.ticks_diff(time.ticks_ms(), entry[Q_TICKS])
    if age < config.DELAY_NOTICE_MS:
        return ''
    return ' (delayed ' + str(age // 1000) + 's)'

def flush_queue():
    """Deliver what is waiting. Stops at the first retryable failure.

    Nothing leaves the queue until Telegram has confirmed it. The previous
    code sent and forgot, so a failed send lost the ring silently -- seen
    on the bench as a logged ring that never arrived.
    """
    sentCount = 0
    while queue and sentCount < config.QUEUE_FLUSH_PER_PASS:
        entry = queue[0]
        outcome, status, body = send_message(chatId, config.text + describe_delay(entry))
        if outcome == net.REQUEST_OK:
            queue.pop(0)
            sentCount += 1
        elif outcome == net.REQUEST_FATAL:
            # Retrying will not help. Drop it rather than block the queue
            # behind something permanently undeliverable.
            queue.pop(0)
            applog.report('Dropped an undeliverable ring: ' +
                   describe_api_error(status, body))
        else:
            # Transient or rate limited. Leave it and try again next pass.
            break
    if sentCount:
        # Local confirmation that a ring reached Telegram. Once per pass, not
        # per ring: a burst flushing together is one visitor's worth of
        # feedback, and blinking N times would just be noise.
        led.led_alert()
    return sentCount

def snapshot_queue():
    """Persist the queue. Writes only if the contents actually changed."""
    global lastSnapshotTicks
    lastSnapshotTicks = time.ticks_ms()
    persist.state_set('queue', [[e[Q_EPOCH], e[Q_WIDTH]] for e in queue])

def maybe_snapshot_queue():
    """Persist a queue that has been waiting long enough to be at risk.

    Not on a timer. A short outage never writes at all, because RAM already
    covers it. Only an outage that has already run for minutes -- long
    enough that a reset in the middle is a real possibility -- earns a
    flash write.
    """
    now = time.ticks_ms()
    if not queue:
        if persist.state_get('queue', []):
            snapshot_queue()      # delivered; clear the copy on flash
        return
    if time.ticks_diff(now, queue[0][Q_TICKS]) < config.QUEUE_SNAPSHOT_AFTER_MS:
        return
    if time.ticks_diff(now, lastSnapshotTicks) < config.QUEUE_SNAPSHOT_MIN_MS:
        return
    snapshot_queue()

def restore_queue():
    """Reload rings that outlived a reset."""
    stored = persist.state_get('queue', [])
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
    applog.report('Recovered ' + str(len(queue)) + ' undelivered ring(s)')

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

def heartbeat_text():
    """Everything worth knowing about a device nobody is watching.

    Free memory leads because it is the one figure that only uptime can
    produce: the socket-leak fix in net.do_request cannot be proven by any
    single reading, and getting one by hand means killing the run to reach
    a REPL -- which the watchdog then resets out from under you.
    """
    gc.collect()
    lines = [
        'Still here.',
        'Uptime: ' + format_uptime(uptimeMs),
        'Boot: #' + str(bootNumber if bootNumber is not None else '?') +
        ' (' + resets.reset_verdict(resetInfo) + ')',
        'Free memory: ' + str(gc.mem_free()) + ' bytes',
        'WiFi: ' + net.wifi_rssi() + ', ' + str(net.wifiData),
        'Clock: ' + clock_status(),
        input_summary(),
        'Flash writes: ' + str(persist.state_get('writes', 0)),
    ]
    if net.networkFailures:
        lines.append('Network: ' + str(net.networkFailures) +
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
    if time.ticks_diff(time.ticks_ms(), lastHeartbeat) < config.HEARTBEAT_MS:
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

    The radio is powered up here (net.power_up) because on the Pico W the
    onboard LED hangs off the CYW43 chip -- machine.Pin('LED') is unusable
    until the wireless interface is powered up.
    """
    global doorBellInput, mac
    net.power_up(wifiPowerSave)
    # The CYW43-hosted LED is unusable until the radio is up, so create it
    # here and hand it to the led module, which sets a known resting state.
    led.init(machine.Pin('LED', machine.Pin.OUT))
    doorBellInput = add_input('Doorbell', doorBellPin)[IN_PIN]
    # MAC lives in the wireless chip OTP. Read it from the interface we
    # already have rather than constructing a second WLAN object.
    mac = ubinascii.hexlify(net.mac_address(), ':').decode()
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
    print(resets.format_reset_info(resetInfo))
    led.set_led_state(led.LED_ERROR)
    while True:
        try:
            # Raw pin access, deliberately not led.blink_led: that feeds the
            # watchdog, and a fatal fault should let the watchdog escalate
            # rather than be held off forever.
            if led.led is not None:
                led.led.on()
                time.sleep(.08)
                led.led.off()
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
    persist.state_set('boots', bootNumber)
    # A sector erase stalls the CPU for tens of milliseconds, occasionally
    # more, and runs with interrupts disabled.
    wdt.feed_watchdog()
    # Attempts since the last stable boot are now history.
    resets.clear_unstable_count()
    # The only flash write in normal operation; a silent one is hard to
    # confirm while validating.
    applog.report('Boot ' + str(bootNumber) + ' stable after ' +
           str(config.BOOT_STABLE_MS // 1000) + 's')

####################################################################################
# Wiring.
#
# net cannot import the layers above it, so main hands it what it needs once,
# at import: the credentials, and the two hooks for things that live above
# it (resetting with a queue snapshot, and snapshotting while a connect
# blocks). At import rather than in boot() so the hooks are in place before
# anything can call into net, under the test harness as much as on device.
####################################################################################

net.ssid = secrets['ssid']
net.pw = secrets['pw']
net.on_give_up = self_reset
net.on_wait = maybe_snapshot_queue

def boot():
    """Bring the device up, hardware first.

    Ordering is the whole point. Previously connect_wifi() ran at module
    scope, outside any try, and sent the startup message immediately after
    association -- exactly when DNS is least likely to be ready. If that
    send raised, the script died before machine.Pin(16) was ever reached
    and the doorbell was dead until someone power-cycled it.

    Now: pins, then flash, then network. Only the first is fatal.
    """
    global resetInfo, bootNumber, bootStableAt, lastHeartbeat
    # First, before anything can fail. The scratch registers are volatile
    # and a later crash would take the evidence with it.
    resetInfo = resets.read_reset_info()
    lastHeartbeat = time.ticks_ms()

    try:
        setup_hardware()
    except Exception as e:
        # No usable input pin means there is nothing to do.
        error_halt('could not configure hardware: ' + str(e))

    persist.load()

    # The running total lives in flash, so it is only knowable once state
    # has loaded -- which is why the summary is logged here rather than at
    # the top of boot(). Nothing is written yet; see mark_boot_stable().
    restore_queue()

    bootNumber = persist.state_get('boots', 0) + 1
    resetInfo['bootNumber'] = bootNumber
    bootStableAt = time.ticks_add(time.ticks_ms(), config.BOOT_STABLE_MS)

    summary = resets.format_reset_info(resetInfo)
    print(summary)
    applog.append_to_log(summary)

    # Everything that could legitimately hang from here on is network work.
    # arm_watchdog returns its status rather than logging, to stay a leaf;
    # report it here.
    armMessage = wdt.arm_watchdog()
    if armMessage:
        applog.report(armMessage)

    try:
        net.connect_wifi()
        # Before announcing, so the startup message and its reset diagnosis
        # carry a real timestamp rather than a relative one. Best effort: a
        # failed sync just leaves the clock unsynced, and the main loop
        # retries. Never fatal.
        timesync.sync_clock()
        # Before announcing: otherwise a reset replays every command
        # Telegram has been holding, including ones from yesterday.
        discard_update_backlog()
        announce_startup()
    except Exception as e:
        # Network trouble is the main loop's problem, not a boot failure.
        applog.append_to_log('Startup networking failed: ' + str(e))
        print('Startup networking failed: ' + str(e))

# Entry-point guard. On the device main.py is __main__, so boot() runs and
# the loop starts exactly as before. Under the test harness main.py is
# imported, not run, so __name__ is 'main' and neither fires: the tests get
# every function and module global without boot() doing real work or the
# loop never returning. This is what lets the suites use `import main`
# instead of AST-stripping the loop out, and what makes the E2 split safe.
if __name__ == '__main__':
    boot()

    while True:
        try:
            if (not net.is_wifi_connected()):
                net.connect_wifi()
                announce_startup()

            if net.networkBounceRequested:
                net.bounce_wifi()

            poll_inputs()
            flush_announcement()
            flush_queue()
            maybe_snapshot_queue()

            # Check for new messages
            if (time.ticks_diff(time.ticks_ms(), lastLogCheck) > config.logCheckInterval):
                # Log only. Printed once a minute it was pure noise, and it
                # crowded out the events worth seeing in a long run.
                applog.append_to_log('Checking for new messages')
                read_message(chatId)
                lastLogCheck = time.ticks_ms()

            mark_boot_stable()
            wdt.feed_watchdog()

            # Record when this pass ran, so was_unpollable() can tell whether
            # a ring landed in a gap the old polling loop could not have
            # covered.
            prevPassTicks = lastPassTicks
            lastPassTicks = time.ticks_ms()
            uptimeMs += time.ticks_diff(lastPassTicks, prevPassTicks)
            timesync.maybe_resync_clock()
            maybe_heartbeat()

            wdt.sleep_fed(config.loopDelay)

        except KeyboardInterrupt:
            print('KeyboardInterrupt')
            if wdt.wdt is not None:
                # Nothing can disarm an RP2040 watchdog. Leaving the loop
                # stops the feeding, so the board resets shortly. That is
                # correct in service (an exited loop is a dead doorbell) but
                # it is worth saying out loud on the bench.
                print('Watchdog is armed: expect a reset within ' +
                      str(config.WDT_TIMEOUT_MS // 1000) + 's')
            break
        except Exception as e:
            print(e)
            net.wlan.disconnect()
            # The link is down now, so the LED must say so. The old code lit
            # the "connected" indicator here, right after disconnecting,
            # leaving it lying until the next reconnect. The next loop pass
            # calls connect_wifi(), which drives the LED back through
            # connecting to connected on its own.
            led.set_led_state(led.LED_OFF)
            applog.append_to_log('WiFi disconnected: ' + str(e))
            # Grace period, in fed slices. Left as a single sleep(10) this
            # would outlast the watchdog and reset the board on every error.
            wdt.sleep_fed(10)
