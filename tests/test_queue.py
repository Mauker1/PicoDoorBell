"""Verify B6: a ring survives a network outage.

The bug: a ring detected while the network was down was logged and then
lost. Seen on the bench as `Doorbell ring, 202ms` followed by `WiFi is
disconnected.` and no Telegram message ever arriving -- detected,
measured, counted, and gone, with no crash and no error.

Reuses the stub hardware from test_boot.py.
"""
import ast
import json
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
Requests = _stubs['Requests']
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


def fresh(at=100000, keep_state=False):
    del sent[:]
    mem32.cells.clear()
    clock[0] = at
    WLAN.connected = True
    Requests.raise_oserror = False
    if not keep_state:
        try:
            os.remove('state.json')
        except OSError:
            pass
    ns = load_firmware()
    ns['send_message'] = recorder(ns)
    return ns


def recorder(ns, outcome=None):
    def send(chat, msg):
        if outcome is not None:
            return (outcome, 500, {})
        if not WLAN.connected or Requests.raise_oserror:
            return (ns['REQUEST_RETRY'], 0, None)
        sent.append(msg)
        return (ns['REQUEST_OK'], 200, {})
    return send


os.chdir(tempfile.mkdtemp())

# --- 1. A ring while the network is down is not lost -----------------------
ns = fresh()
WLAN.connected = False
ns['enqueue_ring'](2000)
ns['flush_queue']()
check('nothing delivered while offline', len(sent), 0)
check('the ring is waiting, not gone', len(ns['queue']), 1)

WLAN.connected = True
ns['flush_queue']()
check('delivered once the network returns', len(sent), 1)
check('queue emptied', len(ns['queue']), 0)

# --- 2. A late delivery says so --------------------------------------------
ns = fresh()
WLAN.connected = False
ns['enqueue_ring'](2000)
clock[0] += 90000                        # 90 s outage
WLAN.connected = True
ns['flush_queue']()
check('a delayed ring is marked as such', 'delayed 90s' in sent[0], True)

ns = fresh()
ns['enqueue_ring'](2000)
ns['flush_queue']()
check('a prompt ring is not marked', 'delayed' in sent[0], False)

# --- 3. Order is preserved -------------------------------------------------
ns = fresh()
WLAN.connected = False
for width in (1000, 2000, 3000):
    ns['enqueue_ring'](width)
    clock[0] += 1000
WLAN.connected = True
ns['flush_queue']()
check('all three delivered', len(sent), 3)

# --- 4. The queue is bounded -----------------------------------------------
ns = fresh()
WLAN.connected = False
for _ in range(ns['QUEUE_MAX'] + 5):
    ns['enqueue_ring'](2000)
    clock[0] += 100
check('queue stops at its limit', len(ns['queue']), ns['QUEUE_MAX'])
check('overflow counted', ns['queueDropped'], 5)

# --- 5. A permanent failure does not block the queue -----------------------
ns = fresh()
ns['enqueue_ring'](2000)
ns['enqueue_ring'](2000)
ns['send_message'] = recorder(ns, outcome=ns['REQUEST_FATAL'])
ns['flush_queue']()
check('undeliverable rings are dropped, not retried forever',
      len(ns['queue']), 0)

# A retryable failure keeps them.
ns = fresh()
ns['enqueue_ring'](2000)
ns['send_message'] = recorder(ns, outcome=ns['REQUEST_RETRY'])
ns['flush_queue']()
check('a retryable failure keeps the ring', len(ns['queue']), 1)

# --- 6. Flash: only after an outage has run long ---------------------------
ns = fresh()
WLAN.connected = False
ns['enqueue_ring'](2000)
ns['maybe_snapshot_queue']()
check('a brief outage writes nothing', state_has_queue := bool(
    json.load(open('state.json'))['queue']) if os.path.exists('state.json') else False,
      False)

clock[0] += ns['QUEUE_SNAPSHOT_AFTER_MS'] + 1000
ns['maybe_snapshot_queue']()
stored = json.load(open('state.json'))['queue']
check('a long outage is persisted', len(stored), 1)

writes_before = json.load(open('state.json'))['writes']
ns['maybe_snapshot_queue']()
ns['maybe_snapshot_queue']()
check('repeated calls do not rewrite',
      json.load(open('state.json'))['writes'], writes_before)

# Once delivered, the flash copy is cleared.
WLAN.connected = True
ns['flush_queue']()
clock[0] += ns['QUEUE_SNAPSHOT_MIN_MS'] + 1000
ns['maybe_snapshot_queue']()
check('the flash copy is cleared after delivery',
      json.load(open('state.json'))['queue'], [])

# --- 6b. A ring arriving during trouble is persisted at once ---------------
# A watchdog bite during a slow DNS lookup would otherwise take it: the
# five-minute snapshot has not fired yet and RAM does not survive.
ns = fresh()
ns['networkFailures'] = 3          # the network is already misbehaving
ns['enqueue_ring'](2000)
stored = json.load(open('state.json'))['queue']
check('a ring queued during trouble is written immediately', len(stored), 1)

# When the network is healthy there is no such urgency, and no write.
ns = fresh()
ns['networkFailures'] = 0
ns['enqueue_ring'](2000)
exists = os.path.exists('state.json')
stored = json.load(open('state.json'))['queue'] if exists else []
check('a ring queued on a healthy network writes nothing', stored, [])

# --- 6c. An announcement is not lost to a failed send ----------------------
ns = fresh()
ns['pendingAnnouncement'] = 'boot report'
ns['send_message'] = recorder(ns, outcome=ns['REQUEST_RETRY'])
ns['flush_announcement']()
check('a failed announcement is retained',
      ns['pendingAnnouncement'], 'boot report')
ns['send_message'] = recorder(ns)
ns['flush_announcement']()
check('and delivered on the next attempt', sent[-1], 'boot report')
check('then cleared', ns['pendingAnnouncement'], None)

# --- 7. The ring survives a reset ------------------------------------------
ns = fresh()
WLAN.connected = False
ns['enqueue_ring'](2345)
clock[0] += ns['QUEUE_SNAPSHOT_AFTER_MS'] + 1000
ns['maybe_snapshot_queue']()
check('persisted before the reset', len(json.load(open('state.json'))['queue']), 1)

# Reboot: same flash, fresh RAM. WiFi must be up for boot() to complete --
# connect_wifi() loops until it connects, which is B4's job to bound.
WLAN.connected = True
mem32.cells.clear()
clock[0] = 500
del sent[:]
ns2 = load_firmware()
ns2['send_message'] = recorder(ns2)
check('the ring came back', len(ns2['queue']), 1)
check('marked as restored', ns2['queue'][0][ns2['Q_RESTORED']], True)

ns2['flush_queue']()
check('and is delivered after the reset', len(sent), 1)
check('described honestly, since its time is unknowable',
      'before a restart' in sent[0], True)

# --- 8. Corrupt queue data must not stop the boot --------------------------
open('state.json', 'w').write(
    '{"v": 3, "chatId": null, "epochAnchor": null, "writes": 1, '
    '"boots": 1, "queue": [["nonsense"], null, 42]}')
WLAN.connected = True
mem32.cells.clear()
ns3 = load_firmware()
check('boot survives a malformed queue', ns3['doorBellInput'] is not None, True)
check('unreadable entries are skipped', len(ns3['queue']), 0)

print()
print('%d/%d passed' % (sum(results), len(results)))
raise SystemExit(0 if all(results) else 1)
