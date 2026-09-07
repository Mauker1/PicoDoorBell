"""Verify C3: the device reports in on its own.

Before this, the only sign of life was a per-minute console print that
said nothing, so silence and death looked identical. Worse, the figure
that matters most -- free memory, the only way to prove the socket-leak
fix -- could only be read by killing the run to reach a REPL, which the
watchdog then reset out from under you.

Reuses the stub hardware from test_boot.py.
"""
import ast
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', 'main.py')

_src = open(os.path.join(HERE, 'test_boot.py')).read()
_stubs = {'__name__': 'stubs', '__file__': os.path.join(HERE, 'test_boot.py')}
exec(compile(_src[:_src.index('work = tempfile.mkdtemp()')], 'stubs', 'exec'), _stubs)
_stubs['install_stubs']()

clock = _stubs['clock']
mem32 = _stubs['mem32']
WLAN = _stubs['WLAN']

results = []


def check(label, got, expected):
    ok = got == expected
    results.append(ok)
    print('%-4s %-54s got=%r' % ('ok' if ok else 'FAIL', label, got))


def load_firmware():
    tree = ast.parse(open(SRC).read())
    tree.body = [n for n in tree.body if not isinstance(n, ast.While)]
    ns = {'__name__': 'main'}
    exec(compile(tree, 'main.py', 'exec'), ns)
    return ns


sent = []


def fresh():
    del sent[:]
    mem32.cells.clear()
    WLAN.connected = True
    clock[0] = 1000
    ns = load_firmware()
    ns['send_message'] = lambda chat, msg: (sent.append(msg),
                                            (ns['REQUEST_OK'], 200, {}))[1]
    return ns


os.chdir(tempfile.mkdtemp())

# --- 1. The report carries what only uptime can show -----------------------
ns = fresh()
ns['uptimeMs'] = 90061000            # 1d 1h 1m
text = ns['heartbeat_text']()

check('it says it is alive', 'Still here.' in text, True)
check('uptime is human readable', '1d 1h 1m' in text, True)
check('free memory is reported', 'Free memory:' in text, True)
check('the boot number is reported', 'Boot: #' in text, True)
check('the input counters are included', 'Doorbell:' in text, True)
check('the queue depth is included', 'queue:' in text, True)
check('flash writes are included', 'Flash writes:' in text, True)
check('it fits one Telegram message', len(text) <= 4096, True)

# --- 2. Uptime formatting ---------------------------------------------------
check('minutes only under an hour', ns['format_uptime'](600000), '10m')
check('hours and minutes under a day', ns['format_uptime'](7260000), '2h 1m')
check('days once past one', ns['format_uptime'](180000000), '2d 2h 0m')

# --- 3. It fires on schedule, not before ------------------------------------
ns = fresh()
ns['lastHeartbeat'] = 0
clock[0] = ns['HEARTBEAT_MS'] - 1000
check('nothing before the interval elapses', ns['maybe_heartbeat'](), False)
clock[0] = ns['HEARTBEAT_MS'] + 1000
check('then it reports', ns['maybe_heartbeat'](), True)
check('via the retrying slot, not a bare send',
      ns['pendingAnnouncement'] is not None, True)
check('and the send itself has not happened yet', len(sent), 0)

ns['flush_announcement']()
check('the flush delivers it', len(sent), 1)
check('and it is the heartbeat', 'Still here.' in sent[0], True)

# --- 4. A failed heartbeat is retried, not lost -----------------------------
# A missing "still alive" message is precisely what a dead device looks like.
ns = fresh()
ns['lastHeartbeat'] = 0
clock[0] = ns['HEARTBEAT_MS'] + 1000
ns['maybe_heartbeat']()
ns['send_message'] = lambda chat, msg: (ns['REQUEST_RETRY'], 0, None)
ns['flush_announcement']()
check('a failed heartbeat is kept', ns['pendingAnnouncement'] is not None, True)

# --- 5. A startup report outranks a heartbeat -------------------------------
ns = fresh()
ns['pendingAnnouncement'] = 'boot report'
ns['lastHeartbeat'] = 0
clock[0] = ns['HEARTBEAT_MS'] + 1000
check('the heartbeat yields', ns['maybe_heartbeat'](), False)
check('the boot report is untouched',
      ns['pendingAnnouncement'], 'boot report')
clock[0] += ns['HEARTBEAT_MS'] + 1000
ns['pendingAnnouncement'] = None
check('and the next one goes out', ns['maybe_heartbeat'](), True)

# --- 6. /status asks for the same thing on demand ---------------------------
# This is what makes the memory figure readable without killing the run.
ns = fresh()
ns['read_message']('chat')
check('status is a known command', ns['statusCommand'], '/status')

# --- 7. The per-minute console print is gone --------------------------------
src = open(SRC).read()
check('no per-minute console noise',
      "print('Checking for new messages...')" in src, False)
check('but it is still in the log for timelines',
      "append_to_log('Checking for new messages')" in src, True)

print()
print('%d/%d passed' % (sum(results), len(results)))
raise SystemExit(0 if all(results) else 1)
