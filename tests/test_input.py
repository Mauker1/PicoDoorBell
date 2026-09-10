"""Verify B1: the doorbell input is latched by interrupt, not polled.

The bug B1 fixes: the input was read once per second in a loop that also
made blocking TLS calls and slept 5 s after a press. A ring landing in one
of those windows was lost silently, because the physical chime still
sounds and nobody can report a notification that never arrived.

Both edges are captured in hardware, so the pulse width is known exactly
even when the main loop was blocked for seconds and only looks afterwards.

Imports main under the host-side stubs (F1).
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', 'main.py')
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, HERE)
import stubs

clock = stubs.clock
mem32 = stubs.mem32
Requests = stubs.Requests

results = []


def check(label, got, expected):
    ok = got == expected
    results.append(ok)
    print('%-4s %-54s got=%r' % ('ok' if ok else 'FAIL', label, got))


sent = []


def fresh(at=100000):
    """Boot the firmware with a recording send_message and a known clock.

    setup_hardware() is called explicitly: boot() no longer runs at import
    (the __name__ guard), so the doorbell input is registered here instead.
    """
    del sent[:]
    mem32.cells.clear()
    clock[0] = at
    m = stubs.load_firmware()
    m.setup_hardware()
    m.send_message = lambda chat, msg: (sent.append(msg), (0, 200, {}))[1]
    m.lastPassTicks = clock[0]
    m.prevPassTicks = clock[0]
    return m, m.inputs[0]


def pulse(entry, m, start, width):
    """Drive a complete ring: rising edge, then falling edge `width` later."""
    pin = entry[m.IN_PIN]
    clock[0] = start
    pin.edge(1)
    clock[0] = start + width
    pin.edge(0)


def pass_loop(m, at):
    """Advance to a main-loop pass and process whatever is latched."""
    clock[0] = at
    m.prevPassTicks = m.lastPassTicks
    m.lastPassTicks = at
    m.poll_inputs()
    m.flush_queue()


os.chdir(tempfile.mkdtemp())
Requests.raise_oserror = False

# --- 1. A real ring, 2 s wide ---------------------------------------------
m, entry = fresh()
check('input registered', len(m.inputs), 1)
check('interrupt armed', entry[m.IN_PIN].handler is not None, True)
check('watches both edges', entry[m.IN_PIN].trigger,
      stubs.Pin.IRQ_RISING | stubs.Pin.IRQ_FALLING)

pulse(entry, m, 100000, 2000)
check('edge latched without the loop running', entry[m.IN_PENDING], True)
check('nothing sent before the loop looks', len(sent), 0)

pass_loop(m, 102500)
check('ring delivered', len(sent), 1)
check('ring counted', entry[m.IN_RINGS], 1)
check('latch cleared', entry[m.IN_PENDING], False)

# --- 2. A transient is not a visitor --------------------------------------
m, entry = fresh()
pulse(entry, m, 100000, 60)
pass_loop(m, 100500)
check('60ms transient rejected', len(sent), 0)
check('transient counted', entry[m.IN_REJECTED], 1)
check('no ring recorded', entry[m.IN_RINGS], 0)

# Just under and just over the threshold.
m, entry = fresh()
pulse(entry, m, 100000, 149)
pass_loop(m, 101000)
check('149ms is below threshold', len(sent), 0)
pulse(entry, m, 110000, 151)
pass_loop(m, 111000)
check('151ms is above threshold', len(sent), 1)

# --- 3. One ring, one alert -----------------------------------------------
m, entry = fresh()
pulse(entry, m, 100000, 2000)
pass_loop(m, 102500)
pulse(entry, m, 103000, 2000)          # impatient second press
pass_loop(m, 105500)
check('second press inside lockout is suppressed', len(sent), 1)
check('but the loop still cleared it', entry[m.IN_PENDING], False)

pulse(entry, m, 120000, 2000)          # well beyond the 5 s lockout
pass_loop(m, 122500)
check('press beyond lockout alerts again', len(sent), 2)

# --- 4. Debounce ----------------------------------------------------------
m, entry = fresh()
pin = entry[m.IN_PIN]
clock[0] = 100000
pin.edge(1)
clock[0] = 100010                        # contact noise, 10 ms later
pin.edge(0)
clock[0] = 100020
pin.edge(1)
check('bouncing edges do not restart the pulse',
      entry[m.IN_RISE], 100000)
check('debounced edges leave it pending', entry[m.IN_PENDING], True)

# --- 4b. A pulse shorter than the debounce window must still close --------
# Regression: one debounce window covered both edges, so a tap under
# DEBOUNCE_MS had its falling edge swallowed. The input stayed latched with
# no width, reported nothing, and sat there until the 15 s stuck timeout.
# Found on hardware: a very short tap produced silence rather than a
# rejection.
m, entry = fresh()
pulse(entry, m, 100000, 20)             # 20 ms, well inside the 50 ms window
check('a sub-debounce pulse still closes', entry[m.IN_COMPLETE], True)
check('its width is recorded',
      m.time.ticks_diff(entry[m.IN_FALL], entry[m.IN_RISE]), 20)
pass_loop(m, 101000)
check('and it is reported as a transient', entry[m.IN_REJECTED], 1)
check('not left latched', entry[m.IN_PENDING], False)
check('no alert sent', len(sent), 0)

# Bounce on the release: the pulse closes on the last fall, not the first.
m, entry = fresh()
pin = entry[m.IN_PIN]
clock[0] = 100000
pin.edge(1)
clock[0] = 102000
pin.edge(0)
clock[0] = 102005                        # bounce back up
pin.edge(1)
clock[0] = 102010
pin.edge(0)
check('release bounce does not split the pulse', entry[m.IN_FALL], 102010)
pass_loop(m, 103000)
check('bouncing release still delivers one ring', len(sent), 1)

# --- 4c. Contact chatter on the *make* must not truncate the press --------
# Found on hardware: a long press reported 'transient ignored, 2ms'. Closing
# on the first falling edge treated make-chatter as the release, judged the
# pulse on those 2 ms, and threw away the real four-second press that
# followed.
m, entry = fresh()
pin = entry[m.IN_PIN]
clock[0] = 100000
pin.edge(1)                              # contact makes
clock[0] = 100002
pin.edge(0)                              # chatter
clock[0] = 100004
pin.edge(1)
clock[0] = 100006
pin.edge(0)                              # more chatter
clock[0] = 100008
pin.edge(1)                              # settles high
check('chatter does not close the pulse', entry[m.IN_COMPLETE], False)
check('original rise time kept', entry[m.IN_RISE], 100000)
pass_loop(m, 101000)
check('mid-pulse, nothing judged yet', len(sent), 0)
clock[0] = 104000
pin.edge(0)                              # actual release, 4 s later
pass_loop(m, 104200)
check('the real press is delivered', len(sent), 1)
check('and measured at its true width',
      entry[m.IN_RINGS], 1)
check('not counted as a transient', entry[m.IN_REJECTED], 0)

# A genuine short tap is still rejected, once the close has settled.
m, entry = fresh()
pulse(entry, m, 100000, 20)
pass_loop(m, 100030)                    # inside the debounce window
check('a close is not judged before it settles', entry[m.IN_PENDING], True)
pass_loop(m, 101000)                    # well after
check('then it is rejected', entry[m.IN_REJECTED], 1)
check('no alert', len(sent), 0)

# --- 5. The whole point: a ring during a blocking call --------------------
# The loop is stuck in a TLS handshake for 4 s. Both edges are still caught
# in hardware, so the ring survives, and is counted as one the old polling
# loop could not have seen.
m, entry = fresh()
pass_loop(m, 100000)
pulse(entry, m, 101000, 2000)           # entirely inside the blocked window
pass_loop(m, 104000)
check('ring during a blocked loop still delivered', len(sent), 1)
check('counted as unpollable', entry[m.IN_MISSED], 1)

# A ring that a poll could have caught is not counted as unpollable.
m, entry = fresh()
pass_loop(m, 100000)
pulse(entry, m, 99500, 2000)            # started before the previous pass
pass_loop(m, 102000)
check('normally pollable ring not counted as missed',
      entry[m.IN_MISSED], 0)
check('but still delivered', len(sent), 1)

# --- 6. Mid-pulse: wait, do not guess -------------------------------------
m, entry = fresh()
pin = entry[m.IN_PIN]
clock[0] = 100000
pin.edge(1)                              # rising edge only, still high
pass_loop(m, 100500)
check('mid-pulse ring is not judged early', len(sent), 0)
check('still latched', entry[m.IN_PENDING], True)
clock[0] = 102000
pin.edge(0)
pass_loop(m, 102100)
check('delivered once the pulse completes', len(sent), 1)

# --- 7. Stuck input is a fault, not a caller ------------------------------
m, entry = fresh()
pin = entry[m.IN_PIN]
clock[0] = 100000
pin.edge(1)
pass_loop(m, 100000 + 16000)            # past STUCK_INPUT_MS, never fell
check('stuck input sends no alert', len(sent), 0)
check('stuck input clears the latch', entry[m.IN_PENDING], False)
check('stuck input not counted as a ring', entry[m.IN_RINGS], 0)

# --- 8. Structure supports a second input ---------------------------------
m, entry = fresh()
second = m.add_input('Flat door', 22)
check('a second input is just another entry', len(m.inputs), 2)
pulse(second, m, 100000, 2000)
pass_loop(m, 102500)
check('second input latches independently', second[m.IN_RINGS], 1)
check('first input untouched', entry[m.IN_RINGS], 0)

# --- 9. Counters are reportable -------------------------------------------
check('summary names both inputs', 'Flat door' in m.input_summary() and
      'Doorbell' in m.input_summary(), True)

print()
print('%d/%d passed' % (sum(results), len(results)))
raise SystemExit(0 if all(results) else 1)
