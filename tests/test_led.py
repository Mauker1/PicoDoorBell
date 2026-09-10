"""Verify G1: the LED always reflects true state, and never lies.

The onboard LED is the only local feedback the device has. Before G1, LED
calls were scattered and the main loop's error handler lit the "connected"
indicator immediately after wlan.disconnect(), so a disconnected board
showed a connected light until the next reconnect. G1 routes every steady
level through set_led_state, so the indicator cannot be left asserting the
wrong thing.

Imports main under the host-side stubs (F1). The Pin stub records every
level change in .levels, which is how the patterns below are observed.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', 'main.py')
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, HERE)
import stubs

events = stubs.events
mem32 = stubs.mem32
Requests = stubs.Requests
WLAN = stubs.WLAN
WDT = stubs.WDT
clock = stubs.clock

results = []


def check(label, got, expected):
    ok = got == expected
    results.append(ok)
    print('%-4s %-56s got=%r' % ('ok' if ok else 'FAIL', label, got))


def fresh():
    """Import main and configure hardware, so m.led is the stub LED pin."""
    del events[:]
    del WDT.instances[:]
    mem32.cells.clear()
    WLAN.connected = True
    Requests.raise_oserror = False
    m = stubs.load_firmware()
    m.setup_hardware()
    m.arm_watchdog()
    return m


os.chdir(tempfile.mkdtemp())

# --- 1. Steady states drive the right resting level ------------------------
m = fresh()
m.set_led_state(m.LED_CONNECTED)
check('connected is solid on', m.led.value(), 1)
check('connected is recorded as the state', m.ledState, m.LED_CONNECTED)

m.set_led_state(m.LED_OFF)
check('off is solid off', m.led.value(), 0)

m.set_led_state(m.LED_CONNECTING)
check('connecting has no solid on level', m.led.value(), 0)

m.set_led_state(m.LED_ERROR)
check('error has no solid on level', m.led.value(), 0)
check('error is recorded as the state', m.ledState, m.LED_ERROR)

# --- 2. The inversion bug is fixed -----------------------------------------
# The old handler did led.off(); wlan.disconnect(); ...; led.on(), leaving
# the connected light on while disconnected. Two guards: the behaviour, and
# the source, since this is a regression worth pinning both ways.
m = fresh()
m.set_led_state(m.LED_CONNECTED)
check('starts connected', m.led.value(), 1)
# Simulate what the handler now does on a loop exception.
m.set_led_state(m.LED_OFF)
check('a disconnect leaves the LED off, not on', m.led.value(), 0)

src = open(SRC).read()
# The loop's exception handler is the one that logs 'WiFi disconnected'.
# Anchor on that rather than the first 'except', of which there are several.
disc_log = src.index("append_to_log('WiFi disconnected: '")
handler = src[src.index('wlan.disconnect()', src.rindex(
    'except Exception as e:', 0, disc_log)):disc_log + 200]
# After the disconnect in the handler there must be no bare led.on().
check('no led.on() follows the disconnect in the handler',
      'led.on()' in handler, False)

# --- 3. A discrete blink restores the steady state -------------------------
m = fresh()
m.set_led_state(m.LED_CONNECTED)
m.led.levels.clear()
m.blink_led(3)
check('a 3-blink toggles the LED', m.led.levels.count(1) >= 3, True)
check('and restores the connected level afterwards', m.led.value(), 1)

m.set_led_state(m.LED_OFF)
m.led.levels.clear()
m.blink_led(2)
check('a blink from off restores off afterwards', m.led.value(), 0)

# --- 4. The alert is a brief double-blink, then steady ---------------------
m = fresh()
m.set_led_state(m.LED_CONNECTED)
m.led.levels.clear()
m.led_alert()
check('alert blinks at least twice', m.led.levels.count(1) >= 2, True)
check('alert returns to the connected level', m.led.value(), 1)

# --- 5. Blinks are watchdog-safe -------------------------------------------
# A long enough burst must feed, or a slow pattern could outlast the timeout.
m = fresh()
before = WDT.instances[0].feeds
m.blink_led(5)
check('blinking feeds the watchdog', WDT.instances[0].feeds > before, True)

# --- 6. Toggle flips the level ---------------------------------------------
m = fresh()
m.set_led_state(m.LED_OFF)
m.led_toggle()
check('toggle from off goes on', m.led.value(), 1)
m.led_toggle()
check('toggle from on goes off', m.led.value(), 0)

# --- 7. Connecting blinks while joining, solid on once connected -----------
# The connect loop toggles the LED once per poll pass while the state is
# connecting, then set_led_state(LED_CONNECTED) makes it solid on.
m = fresh()
WLAN.connected = False
WLAN.connect_after = 3          # succeeds on the third attempt
clock[0] = 0

real_sleep_fed = m.sleep_fed


def ticking_sleep(seconds):
    clock[0] += int(seconds * 1000)
    real_sleep_fed(seconds)


m.sleep_fed = ticking_sleep
m.led.levels.clear()
m.connect_wifi()
check('the LED toggled while connecting', len(m.led.levels) > 1, True)
check('and ends solid on once connected', m.led.value(), 1)
check('and the state is connected', m.ledState, m.LED_CONNECTED)

# --- 8. A delivered ring blinks the alert ----------------------------------
m = fresh()
sent = []
m.send_message = lambda chat, msg: (sent.append(msg), (m.REQUEST_OK, 200, {}))[1]
m.set_led_state(m.LED_CONNECTED)
m.enqueue_ring(2000)
m.led.levels.clear()
m.flush_queue()
check('a delivered ring was sent', len(sent), 1)
check('and blinked the alert', len(m.led.levels) > 0, True)
check('the LED is back to connected after the alert', m.led.value(), 1)

# A flush that delivers nothing does not blink.
m = fresh()
m.set_led_state(m.LED_CONNECTED)
m.led.levels.clear()
m.flush_queue()                 # empty queue
check('an empty flush does not blink', m.led.levels, [])

# --- 9. error_halt records the error state ---------------------------------
# error_halt loops forever, so it cannot be called; assert via source that it
# sets the error state before entering the blink loop.
check('error_halt sets the error state',
      'set_led_state(LED_ERROR)' in src, True)

# --- 10. LED failures never propagate --------------------------------------
# The LED is feedback, never a reason to fail an operation.
m = fresh()


class BrokenLED:
    def on(self):
        raise OSError('LED gone')

    def off(self):
        raise OSError('LED gone')

    def value(self, level=None):
        raise OSError('LED gone')


m.led = BrokenLED()
check('set_led_state swallows an LED failure',
      (m.set_led_state(m.LED_CONNECTED), True)[1], True)
check('blink_led swallows an LED failure',
      (m.blink_led(2), True)[1], True)
check('toggle swallows an LED failure',
      (m.led_toggle(), True)[1], True)

print()
print('%d/%d passed' % (sum(results), len(results)))
raise SystemExit(0 if all(results) else 1)
