"""Verify C3: the device reports in on its own.

Before this, the only sign of life was a per-minute console print that
said nothing, so silence and death looked identical. Worse, the figure
that matters most (free memory, the only way to prove the socket-leak
fix) could only be read by killing the run to reach a REPL, which the
watchdog then reset out from under you.

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
WLAN = stubs.WLAN

results = []


def check(label, got, expected):
    ok = got == expected
    results.append(ok)
    print('%-4s %-54s got=%r' % ('ok' if ok else 'FAIL', label, got))


sent = []


def fresh():
    del sent[:]
    mem32.cells.clear()
    WLAN.connected = True
    clock[0] = 1000
    m = stubs.load_firmware()
    # boot() no longer runs at import (the __name__ guard), so the input
    # registered by setup_hardware() is absent unless we ask for it. The
    # heartbeat's input_summary() needs it to report a Doorbell line.
    m.setup_hardware()
    m.send_message = lambda chat, msg: (sent.append(msg),
                                        (m.REQUEST_OK, 200, {}))[1]
    return m


os.chdir(tempfile.mkdtemp())

# --- 1. The report carries what only uptime can show -----------------------
m = fresh()
m.uptimeMs = 90061000            # 1d 1h 1m
text = m.heartbeat_text()

check('it says it is alive', 'Still here.' in text, True)
check('uptime is human readable', '1d 1h 1m' in text, True)
check('free memory is reported', 'Free memory:' in text, True)
check('the boot number is reported', 'Boot: #' in text, True)
check('the input counters are included', 'Doorbell:' in text, True)
check('the queue depth is included', 'queue:' in text, True)
check('flash writes are included', 'Flash writes:' in text, True)
check('it fits one Telegram message', len(text) <= 4096, True)

# --- 2. Uptime formatting ---------------------------------------------------
check('minutes only under an hour', m.format_uptime(600000), '10m')
check('hours and minutes under a day', m.format_uptime(7260000), '2h 1m')
check('days once past one', m.format_uptime(180000000), '2d 2h 0m')

# --- 3. It fires on schedule, not before ------------------------------------
m = fresh()
m.lastHeartbeat = 0
clock[0] = m.HEARTBEAT_MS - 1000
check('nothing before the interval elapses', m.maybe_heartbeat(), False)
clock[0] = m.HEARTBEAT_MS + 1000
check('then it reports', m.maybe_heartbeat(), True)
check('via the retrying slot, not a bare send',
      m.pendingAnnouncement is not None, True)
check('and the send itself has not happened yet', len(sent), 0)

m.flush_announcement()
check('the flush delivers it', len(sent), 1)
check('and it is the heartbeat', 'Still here.' in sent[0], True)

# --- 4. A failed heartbeat is retried, not lost -----------------------------
# A missing "still alive" message is precisely what a dead device looks like.
m = fresh()
m.lastHeartbeat = 0
clock[0] = m.HEARTBEAT_MS + 1000
m.maybe_heartbeat()
m.send_message = lambda chat, msg: (m.REQUEST_RETRY, 0, None)
m.flush_announcement()
check('a failed heartbeat is kept', m.pendingAnnouncement is not None, True)

# --- 5. A startup report outranks a heartbeat -------------------------------
m = fresh()
m.pendingAnnouncement = 'boot report'
m.lastHeartbeat = 0
clock[0] = m.HEARTBEAT_MS + 1000
check('the heartbeat yields', m.maybe_heartbeat(), False)
check('the boot report is untouched',
      m.pendingAnnouncement, 'boot report')
clock[0] += m.HEARTBEAT_MS + 1000
m.pendingAnnouncement = None
check('and the next one goes out', m.maybe_heartbeat(), True)

# --- 6. /status asks for the same thing on demand ---------------------------
# This is what makes the memory figure readable without killing the run.
m = fresh()
m.read_message('chat')
check('status is a known command', m.statusCommand, '/status')

# --- 7. The per-minute console print is gone --------------------------------
src = open(SRC).read()
check('no per-minute console noise',
      "print('Checking for new messages...')" in src, False)
check('but it is still in the log for timelines',
      "append_to_log('Checking for new messages')" in src, True)

print()
print('%d/%d passed' % (sum(results), len(results)))
raise SystemExit(0 if all(results) else 1)
