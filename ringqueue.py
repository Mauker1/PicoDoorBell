####################################################################################
# B6 undelivered-ring queue: storage and persistence (E2).
#
# A ring detected during a network outage used to be logged and then lost.
# Rings now wait in RAM until they can be sent.
#
# RAM first: the queue exists to survive a *network* outage, which RAM covers
# completely. Flash only helps across a reset, so it is written only when an
# outage has already run long -- a Tier 3 write, event-driven and rare.
#
# This module is the queue itself: its entries, enqueueing (with the drop-
# oldest bound), and the snapshot and restore through persist. It does not
# send. Delivery (flush_queue, and the delay wording on each ring) belongs to
# the Telegram side, which pops entries once Telegram confirms them.
#
# Dependencies: config, applog, persist, clockmod, and net, read only for
# networkFailures (a ring that arrives while the network is already failing
# is snapshotted at once). Nothing above, so the input side can enqueue
# without depending on Telegram.
####################################################################################

import time
import config
import applog
import persist
import clockmod
import net

# Queue sizing and snapshot timings live in config.py.

Q_TICKS = 0      # ticks_ms when it was latched; meaningless across a reset
Q_WIDTH = 1      # pulse width, for the log
Q_EPOCH = 2      # wall-clock seconds, or None until C1 lands
Q_RESTORED = 3   # came back from flash, so its age is unknowable

queue = []
queueDropped = 0
lastSnapshotTicks = 0


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
