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

# In memory log
log = ''

# Wifi connection status
wifiData = ''

# Telegram update id for offset
updateId = 0

# Flags
isStartup = True

# Time variables
startupTime = time.ticks_ms()
lastLogCheck = startupTime
logCheckInterval = 60000
logMaxSize = 10000

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
REQUEST_OK = 0          # 2xx, body decoded
REQUEST_RETRY = 1       # transient (network fault or 5xx), safe to retry
REQUEST_RATE_LIMIT = 2  # 429, honour retry_after before retrying
REQUEST_FATAL = 3       # other 4xx, retrying will not help

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

def do_request(method, url, payload=None):
    """Perform an HTTP request and always release the socket.

    Returns (outcome, status, body), where body is the decoded JSON
    response or None. Never raises for network or HTTP-level failures --
    callers branch on outcome instead.
    """
    response = None
    # A handshake can run into seconds; the watchdog must not bite mid-request.
    feed_watchdog()
    try:
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
        return (classify_response(status), status, body)
    except OSError as e:
        # DNS failure, refused connection, TLS failure, timeout.
        append_to_log('HTTP ' + method + ' failed: ' + str(e))
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
    if outcome != REQUEST_OK:
        append_to_log('sendMessage failed: ' + describe_api_error(status, body))
    return (outcome, status, body)

def read_message(chatId):
    global updateId
    url = ''
    if (updateId != 0):
        url = getURL + "?offset=" + str(updateId) + "?chat_id=" + str(chatId)
    else:
        url = getURL + "?chat_id=" + str(chatId)
    # NOTE: never print or log `url` -- it embeds the bot token.
    outcome, status, body = do_request('GET', url)
    if outcome != REQUEST_OK:
        append_to_log('getUpdates failed: ' + describe_api_error(status, body))
        return
    for result in body['result']:
        updateId = result['update_id'] + 1
        print(result['channel_post']['text'])
        print(result['channel_post']['text'] == logCommand)
        if (result['channel_post']['text'] == logCommand):
            print_log(chatId)

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
    global log
    if (len(log) <= logMaxSize):
        log += str(time.ticks_ms()) + ' ' + message + '\n'
    else:
        print('Log is full. Not appending message.')

def print_log(chatId):
    global log
    print(log)
    send_message(chatId, log)
    if (len(log) > logMaxSize):
        reset_log()

def reset_log():
    global log, wifiData
    log = str(time.ticks_ms()) + ' ' + wifiData + '\n'

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
STATE_VERSION = 2

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
    # Migrations chain here, oldest first. v1 -> v2 added 'boots' and needs
    # no body: the merge below fills absent keys from defaults, so an older
    # file simply starts counting from zero. The version bump still matters,
    # so a downgraded build recognises the file as newer and leaves it alone.
    # Later migrations that do need a body go here:
    #   if version < 3:
    #       data['newField'] = derive_from(data)
    #       version = 3
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
    if info['warmBoot']:
        # Scratch survived, so nothing reset the chip. Soft reboot, or a
        # watchdog bite once B2 exists. CHIP_RESET here is stale.
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

def connect_wifi():
    """Bring the WiFi link up. Connection only -- no notifications.

    Callers decide whether to announce; mixing the two made a transport
    failure look like a connection failure.

    NOTE: still loops until connected. B4 adds the timeout, backoff and
    status interpretation.
    """
    global wifiData
    while True:
        if (is_wifi_connected()):
            blink_onboard_led(3)
            led.on()
            status = wlan.ifconfig()
            print('ip = ' + status[0])
            wifiData = 'WiFi connected. IP: ' + status[0]
            append_to_log(wifiData)
            return True
        else:
            message = 'WiFi is disconnected. Trying to connect.'
            append_to_log(message)
            print(message)
            led.off()
            wlan.connect(ssid, pw)
            sleep_fed(3)

def announce_startup():
    """Tell the chat we are up. Best effort -- never fatal.

    Sending this used to live inside connect_wifi(), where a failure --
    typically DNS not yet ready straight after association -- killed the
    boot before the doorbell input was ever configured.
    """
    global isStartup
    if (isStartup):
        # Carry the reset diagnosis into the chat. The reboots being chased
        # happen on the production unit, not the bench, so this message is
        # the only place the evidence reliably surfaces.
        outcome, status, body = send_message(
            chatId, startupText + '\n' + format_reset_info(resetInfo))
        isStartup = False
    else:
        # Deliberately no reset summary. Nothing reset -- the network came
        # back. Repeating the last boot's diagnosis here made a WiFi blip
        # look like a reboot, which corrupts the very dataset C4 collects.
        outcome, status, body = send_message(chatId, reconnectText)
    return outcome == REQUEST_OK

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
    send_message(chatId, text)

def poll_inputs():
    for entry in inputs:
        if entry[IN_PENDING]:
            process_input(entry)

def input_summary():
    """One line per input, for the heartbeat (C3)."""
    parts = []
    for entry in inputs:
        parts.append(entry[IN_NAME] + ': ' + str(entry[IN_RINGS]) +
                     ' rings, ' + str(entry[IN_REJECTED]) +
                     ' transients, ' + str(entry[IN_MISSED]) +
                     ' unpollable')
    return '; '.join(parts)

def setup_hardware():
    """Configure the pins. Must happen before anything network-related.

    wlan.active(True) lives here because on the Pico W the onboard LED
    hangs off the CYW43 chip -- machine.Pin('LED') is unusable until the
    wireless interface is powered up.
    """
    global led, doorBellInput, mac
    wlan.active(True)
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
    global state, resetInfo, bootNumber, bootStableAt
    # First, before anything can fail. The scratch registers are volatile
    # and a later crash would take the evidence with it.
    resetInfo = read_reset_info()

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
        
        poll_inputs()
        
        # Check for new messages
        if (time.ticks_diff(time.ticks_ms(), lastLogCheck) > logCheckInterval):
            print('Checking for new messages...')
            read_message(chatId)
            lastLogCheck = time.ticks_ms()
        
        mark_boot_stable()
        feed_watchdog()

        # Record when this pass ran, so was_unpollable() can tell whether a
        # ring landed in a gap the old polling loop could not have covered.
        prevPassTicks = lastPassTicks
        lastPassTicks = time.ticks_ms()

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
        print(log)
        # Grace period, in fed slices. Left as a single sleep(10) this
        # would outlast the watchdog and reset the board on every error.
        sleep_fed(10)
        led.on()
        pass
