"""Verify B6: a ring survives a network outage.

The bug: a ring detected while the network was down was logged and then
lost. Seen on the bench as `Doorbell ring, 202ms` followed by `WiFi is
disconnected.` and no Telegram message ever arriving: detected,
measured, counted, and gone, with no crash and no error.

Imports main under the host-side stubs (F1).
"""
import json
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
WLAN = stubs.WLAN

results = []


def check(label, got, expected):
    ok = got == expected
    results.append(ok)
    print('%-4s %-54s got=%r' % ('ok' if ok else 'FAIL', label, got))


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
    m = stubs.load_firmware()
    m.send_message = recorder(m)
    return m


def reboot():
    """Simulate a reset: fresh RAM, same flash, and the queue restored.

    boot() no longer runs at import (the __name__ guard), so the two things
    a reboot does that this suite cares about, registering the input and
    restoring any persisted queue, are called explicitly. main.state must be
    reloaded first, since restore_queue reads from it.
    """
    m = stubs.load_firmware()
    m.state = m.load_state()
    m.setup_hardware()
    m.restore_queue()
    m.send_message = recorder(m)
    return m


def recorder(m, outcome=None):
    def send(chat, msg):
        if outcome is not None:
            return (outcome, 500, {})
        if not WLAN.connected or Requests.raise_oserror:
            return (m.REQUEST_RETRY, 0, None)
        sent.append(msg)
        return (m.REQUEST_OK, 200, {})
    return send


os.chdir(tempfile.mkdtemp())

# --- 1. A ring while the network is down is not lost -----------------------
m = fresh()
WLAN.connected = False
m.enqueue_ring(2000)
m.flush_queue()
check('nothing delivered while offline', len(sent), 0)
check('the ring is waiting, not gone', len(m.queue), 1)

WLAN.connected = True
m.flush_queue()
check('delivered once the network returns', len(sent), 1)
check('queue emptied', len(m.queue), 0)

# --- 2. A late delivery says so --------------------------------------------
m = fresh()
WLAN.connected = False
m.enqueue_ring(2000)
clock[0] += 90000                        # 90 s outage
WLAN.connected = True
m.flush_queue()
check('a delayed ring is marked as such', 'delayed 90s' in sent[0], True)

m = fresh()
m.enqueue_ring(2000)
m.flush_queue()
check('a prompt ring is not marked', 'delayed' in sent[0], False)

# --- 3. Order is preserved -------------------------------------------------
m = fresh()
WLAN.connected = False
for width in (1000, 2000, 3000):
    m.enqueue_ring(width)
    clock[0] += 1000
WLAN.connected = True
m.flush_queue()
check('all three delivered', len(sent), 3)

# --- 4. The queue is bounded -----------------------------------------------
m = fresh()
WLAN.connected = False
for _ in range(m.config.QUEUE_MAX + 5):
    m.enqueue_ring(2000)
    clock[0] += 100
check('queue stops at its limit', len(m.queue), m.config.QUEUE_MAX)
check('overflow counted', m.queueDropped, 5)

# --- 5. A permanent failure does not block the queue -----------------------
m = fresh()
m.enqueue_ring(2000)
m.enqueue_ring(2000)
m.send_message = recorder(m, outcome=m.REQUEST_FATAL)
m.flush_queue()
check('undeliverable rings are dropped, not retried forever',
      len(m.queue), 0)

# A retryable failure keeps them.
m = fresh()
m.enqueue_ring(2000)
m.send_message = recorder(m, outcome=m.REQUEST_RETRY)
m.flush_queue()
check('a retryable failure keeps the ring', len(m.queue), 1)

# --- 6. Flash: only after an outage has run long ---------------------------
m = fresh()
WLAN.connected = False
m.enqueue_ring(2000)
m.maybe_snapshot_queue()
check('a brief outage writes nothing', bool(
    json.load(open('state.json'))['queue']) if os.path.exists('state.json') else False,
      False)

clock[0] += m.config.QUEUE_SNAPSHOT_AFTER_MS + 1000
m.maybe_snapshot_queue()
stored = json.load(open('state.json'))['queue']
check('a long outage is persisted', len(stored), 1)

writes_before = json.load(open('state.json'))['writes']
m.maybe_snapshot_queue()
m.maybe_snapshot_queue()
check('repeated calls do not rewrite',
      json.load(open('state.json'))['writes'], writes_before)

# Once delivered, the flash copy is cleared.
WLAN.connected = True
m.flush_queue()
clock[0] += m.config.QUEUE_SNAPSHOT_MIN_MS + 1000
m.maybe_snapshot_queue()
check('the flash copy is cleared after delivery',
      json.load(open('state.json'))['queue'], [])

# --- 6b. A ring arriving during trouble is persisted at once ---------------
# A watchdog bite during a slow DNS lookup would otherwise take it: the
# five-minute snapshot has not fired yet and RAM does not survive.
m = fresh()
m.networkFailures = 3          # the network is already misbehaving
m.enqueue_ring(2000)
stored = json.load(open('state.json'))['queue']
check('a ring queued during trouble is written immediately', len(stored), 1)

# When the network is healthy there is no such urgency, and no write.
m = fresh()
m.networkFailures = 0
m.enqueue_ring(2000)
exists = os.path.exists('state.json')
stored = json.load(open('state.json'))['queue'] if exists else []
check('a ring queued on a healthy network writes nothing', stored, [])

# --- 6c. An announcement is not lost to a failed send ----------------------
m = fresh()
m.pendingAnnouncement = 'boot report'
m.send_message = recorder(m, outcome=m.REQUEST_RETRY)
m.flush_announcement()
check('a failed announcement is retained',
      m.pendingAnnouncement, 'boot report')
m.send_message = recorder(m)
m.flush_announcement()
check('and delivered on the next attempt', sent[-1], 'boot report')
check('then cleared', m.pendingAnnouncement, None)

# --- 7. The ring survives a reset ------------------------------------------
m = fresh()
WLAN.connected = False
m.enqueue_ring(2345)
clock[0] += m.config.QUEUE_SNAPSHOT_AFTER_MS + 1000
m.maybe_snapshot_queue()
check('persisted before the reset', len(json.load(open('state.json'))['queue']), 1)

# Reboot: same flash, fresh RAM.
WLAN.connected = True
mem32.cells.clear()
clock[0] = 500
del sent[:]
m2 = reboot()
check('the ring came back', len(m2.queue), 1)
check('marked as restored', m2.queue[0][m2.Q_RESTORED], True)

m2.flush_queue()
check('and is delivered after the reset', len(sent), 1)
check('described honestly, since its time is unknowable',
      'before a restart' in sent[0], True)

# --- 8. Corrupt queue data must not stop the boot --------------------------
open('state.json', 'w').write(
    '{"v": 3, "chatId": null, "epochAnchor": null, "writes": 1, '
    '"boots": 1, "queue": [["nonsense"], null, 42]}')
WLAN.connected = True
mem32.cells.clear()
m3 = reboot()
check('boot survives a malformed queue', m3.doorBellInput is not None, True)
check('unreadable entries are skipped', len(m3.queue), 0)

print()
print('%d/%d passed' % (sum(results), len(results)))
raise SystemExit(0 if all(results) else 1)
