####################################################################################
# C1 wall clock: the pure read side (E2).
#
# ticks_ms answers "how long since boot", never "what time is it", and it wraps
# at ~12.4 days. NTP gives real time, but polling it per event would be absurd,
# so one sync captures an anchor: the epoch at a known ticks_ms, from which any
# later wall-clock time is anchor + elapsed. ticks_diff makes the elapsed part
# wrap-safe, so the derived clock outlives the raw counter that feeds it.
#
# This module is the pure read side: the RAM anchor and the functions that read
# and format it. It imports only time, so it is a leaf anything may depend on
# (in particular applog's log_prefix). The sync side (sync_clock,
# maybe_resync_clock) lives with the caller that has NTP, net, persist and
# report; it drives this module through set_anchor(). Keeping the two apart is
# what stops applog to clock to telegram to applog from being a cycle.
#
# The flash copy of the anchor (epochAnchor in state.json) is the sync side's
# concern, not this module's: a coarse fallback for aging rings after a reset.
# set_anchor here touches only RAM.
####################################################################################

import time

# The live anchor: epoch captured at a known ticks_ms. None until a sync lands.
# Read through clock_now(), never directly.
clockAnchorEpoch = None
clockAnchorTicks = 0
# True once at least one sync has ever succeeded this run. Distinguishes
# "never synced" from "synced but now overdue", which the heartbeat reads
# differently. The sync side owns the resync timer; this flag lives here
# because it is anchor state.
clockEverSynced = False


def set_anchor(epoch):
    """Record a fresh (epoch, ticks) anchor in RAM.

    Pure: it touches only this module's state. The flash copy for restored
    ring aging is the sync caller's job, so this stays a leaf.
    """
    global clockAnchorEpoch, clockAnchorTicks, clockEverSynced
    clockAnchorEpoch = epoch
    clockAnchorTicks = time.ticks_ms()
    clockEverSynced = True


def clock_now():
    """Current wall-clock epoch (MicroPython epoch), or None if unsynced.

    Derived from the anchor rather than read from anything: epoch at the
    anchor, plus however long ticks_ms says has passed since. ticks_diff
    keeps the elapsed term correct across the ticks_ms wrap, so this clock
    survives longer than the raw counter that feeds it.
    """
    if clockAnchorEpoch is None:
        return None
    elapsed = time.ticks_diff(time.ticks_ms(), clockAnchorTicks)
    # ticks_ms can read microscopically behind a stored value across a wrap
    # boundary; never let the clock run backwards.
    if elapsed < 0:
        elapsed = 0
    return clockAnchorEpoch + elapsed // 1000


def clock_is_live():
    """True when a real time is available."""
    return clockAnchorEpoch is not None


def format_timestamp(epoch, utcOffset=0):
    """Render an epoch as 'YYYY-MM-DD HH:MM:SS +ZZZZ', local per utcOffset.

    The stored epoch is UTC; the offset is applied here at display time only,
    and is passed in (defaulting to UTC) rather than read from board, so this
    module stays a pure leaf. The trailing offset tag makes the applied zone
    explicit, so a fixed offset that has fallen out of step with daylight
    saving is visible rather than silently wrong.
    """
    if epoch is None:
        return 'unsynced'
    local = epoch + utcOffset
    # time.gmtime on a UTC-plus-offset value yields local wall-clock parts
    # without needing the port to know any timezone.
    t = time.gmtime(local)
    sign = '+' if utcOffset >= 0 else '-'
    off = abs(utcOffset)
    tag = '%s%02d%02d' % (sign, off // 3600, (off % 3600) // 60)
    return '%04d-%02d-%02d %02d:%02d:%02d %s' % (
        t[0], t[1], t[2], t[3], t[4], t[5], tag)
