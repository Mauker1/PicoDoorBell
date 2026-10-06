####################################################################################
# C1 wall clock: the NTP sync side (E2).
#
# clockmod.py holds the anchor and the pure time math; this module gets the
# anchor from the network. One sync captures the epoch at a known ticks_ms,
# and every later wall-clock time is derived from it. Resyncs run on a timer
# (NTP_RESYNC_MS), each one also refreshing the coarse epochAnchor copy in
# state.json that ages rings recovered after a reset.
#
# NTP sync is never fatal: a device with no clock still answers the door, and
# every timestamp simply stays marked as relative until a sync lands.
#
# Dependencies: config, applog, wdt, net, persist, clockmod. Nothing above.
# The display offset for the "Clock synced" line is set by main at import,
# the same way as applog.utcOffset.
####################################################################################

import time
# C1: NTP wall clock. ntptime ships with the rp2 port but not with host-side
# CPython, and a build could in principle lack it. Absence is not fatal: the
# clock simply never syncs and every timestamp stays marked as relative.
try:
    import ntptime
except ImportError:
    ntptime = None
import config
import applog
import wdt
import net
import persist
import clockmod

# Display offset for the sync report, in seconds. Set by main at import.
utcOffset = 0

# The resync timer: ticks_ms of the last good sync.
lastNtpSync = 0

def sync_clock():
    """Sync the wall clock from NTP. Best effort, never fatal, never raises.

    Returns True on a good sync. A failure leaves any existing anchor in
    place: a stale clock beats no clock, and the entries stay tagged with
    their age since last sync through the heartbeat rather than silently
    presenting drift as truth.

    Sets the RAM anchor through clockmod, then writes the coarse flash copy
    (epochAnchor) here, since persistence is this side's concern, not the
    pure clock module's.
    """
    global lastNtpSync
    if ntptime is None:
        return False
    if not net.is_wifi_connected():
        return False
    # Bracket the blocking UDP call with feeds; set the module timeout low so
    # a dead NTP server cannot approach the watchdog ceiling.
    wdt.feed_watchdog()
    try:
        ntptime.timeout = config.NTP_TIMEOUT_S
    except Exception:
        # Older ntptime without a configurable timeout. The watchdog remains
        # the backstop, exactly as for urequests.
        pass
    try:
        epoch = ntptime.time()
    except Exception as e:
        applog.append_to_log('NTP sync failed: ' + str(e))
        wdt.feed_watchdog()
        return False
    wdt.feed_watchdog()
    if epoch < config.EPOCH_SANITY_FLOOR_MP:
        # A stalled read can return 0 or a tiny value. Anchoring to that
        # would date every ring to the epoch, which is worse than no clock.
        applog.append_to_log('NTP returned an implausible epoch; ignoring it')
        return False
    lastNtpSync = time.ticks_ms()
    clockmod.set_anchor(epoch)
    # Coarse flash fallback for aging rings recovered after a reset. Written
    # here, not in clockmod, which stays a pure leaf.
    persist.state_set('epochAnchor', epoch)
    applog.report('Clock synced: ' +
           clockmod.format_timestamp(clockmod.clock_now(), utcOffset))
    return True

def maybe_resync_clock():
    """Resync on the timer, or take a first sync as soon as one is possible.

    Timer-gated like the heartbeat. Each successful resync refreshes the
    coarse epochAnchor copy (written by sync_clock()), so it costs one flash
    write per NTP_RESYNC_MS of uptime: the one deliberate exception to the
    Tier 2 rule, see persist.py.
    An unsynced clock retries every pass it can, which is cheap: the guards
    in sync_clock() return before any network work when WiFi is down.
    """
    if not clockmod.clockEverSynced:
        return sync_clock()
    if time.ticks_diff(time.ticks_ms(), lastNtpSync) < config.NTP_RESYNC_MS:
        return False
    return sync_clock()
