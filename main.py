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

# GPIO wired to the optocoupler output. E1 will move this to config.py.
doorBellPin = 16

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

# Delays
loopDelay = 1
buttonDelay = 5

# Button pressed value
pressed = 1

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
STATE_VERSION = 1

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
    # Future migrations chain here, oldest first:
    #   if version < 2:
    #       data['newField'] = derive_from(data)
    #       version = 2
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
SCRATCH_BOOTCOUNT_IDX = 3
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
        'bootCount': 0,
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
            # Magic intact: the registers kept their contents, so this was
            # a warm reset rather than a power cycle.
            info['warmBoot'] = True
            info['bootCount'] = scratch_read(SCRATCH_BOOTCOUNT_IDX) + 1
        else:
            info['bootCount'] = 1
            scratch_write(SCRATCH_MAGIC_IDX, SCRATCH_MAGIC)
        scratch_write(SCRATCH_BOOTCOUNT_IDX, info['bootCount'])
    except Exception:
        pass
    return info

def format_reset_info(info):
    if info is None:
        return 'Reset info unavailable'
    parts = ['boot #' + str(info['bootCount']), 'cause=' + str(info['cause'])]
    if info['flags']:
        parts.append('chip=' + '+'.join(info['flags']))
    if info['chipReset'] is not None:
        parts.append('raw=0x%08x' % info['chipReset'])
    if info['wdtReason']:
        parts.append('wdt=0x%x' % info['wdtReason'])
    parts.append('warm' if info['warmBoot'] else 'cold')
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
            time.sleep(3)

def announce_startup():
    """Tell the chat we are up. Best effort -- never fatal.

    Sending this used to live inside connect_wifi(), where a failure --
    typically DNS not yet ready straight after association -- killed the
    boot before the doorbell input was ever configured.
    """
    global isStartup
    # Carry the reset diagnosis into the chat. The reboots we are chasing
    # happen on the production unit, not the bench, so the message is the
    # only place the evidence reliably surfaces.
    detail = '\n' + format_reset_info(resetInfo)
    if (isStartup):
        outcome, status, body = send_message(chatId, startupText + detail)
        isStartup = False
    else:
        outcome, status, body = send_message(chatId, reconnectText + detail)
    return outcome == REQUEST_OK

def setup_hardware():
    """Configure the pins. Must happen before anything network-related.

    wlan.active(True) lives here because on the Pico W the onboard LED
    hangs off the CYW43 chip -- machine.Pin('LED') is unusable until the
    wireless interface is powered up.
    """
    global led, doorBellInput, mac
    wlan.active(True)
    led = machine.Pin('LED', machine.Pin.OUT)
    doorBellInput = machine.Pin(doorBellPin, machine.Pin.IN, machine.Pin.PULL_DOWN)
    # MAC lives in the wireless chip OTP. Read it from the interface we
    # already have rather than constructing a second WLAN object.
    mac = ubinascii.hexlify(wlan.config('mac'), ':').decode()
    print('mac = ' + mac)
    print('Doorbell input ready on GP' + str(doorBellPin))

def error_halt(message):
    """Signal an unrecoverable setup fault on the LED.

    Reached only when the board cannot be configured at all, which in
    practice means a bad pin number. A human has to fix it, so blink
    rather than reset. B2 will let the watchdog escalate this.
    """
    print('FATAL: ' + message)
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

def boot():
    """Bring the device up, hardware first.

    Ordering is the whole point. Previously connect_wifi() ran at module
    scope, outside any try, and sent the startup message immediately after
    association -- exactly when DNS is least likely to be ready. If that
    send raised, the script died before machine.Pin(16) was ever reached
    and the doorbell was dead until someone power-cycled it.

    Now: pins, then flash, then network. Only the first is fatal.
    """
    global state, resetInfo
    # First, before anything can fail. The scratch registers are volatile
    # and a later crash would take the evidence with it.
    resetInfo = read_reset_info()
    summary = format_reset_info(resetInfo)
    print(summary)
    append_to_log(summary)

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
        
        if (doorBellInput.value() == pressed):
            print('Doorbell pressed!')
            send_message(chatId, text)
            time.sleep(buttonDelay)
        
        # Check for new messages
        if (time.ticks_diff(time.ticks_ms(), lastLogCheck) > logCheckInterval):
            print('Checking for new messages...')
            read_message(chatId)
            lastLogCheck = time.ticks_ms()
        
        time.sleep(loopDelay)
        
    
    except KeyboardInterrupt:
        print('KeyboardInterrupt')
        break
    except Exception as e:
        print(e)
        led.off()
        wlan.disconnect()
        append_to_log('WiFi disconnected: ' + str(e))
        print(log)
        # Grace period.
        time.sleep(10)
        led.on()
        pass
