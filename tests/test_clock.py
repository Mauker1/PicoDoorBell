"""Verify C1: the NTP wall clock, its fallback, and timestamped output.

The feature exists so that a log line and a queued ring can say *when*,
not merely how long since boot. ticks_ms wraps at ~12.4 days and means
nothing in an incident report; the reboot investigation on the production
unit is easier to correlate against real times than against relative ones.

The design is an anchor, not repeated polling: one NTP read captures the
epoch at a known ticks_ms, and every later time is anchor + elapsed. These
tests drive that anchor through its states: never synced, freshly synced,
advancing, resynced, and fed deliberately bad input.

Imports main under the host-side stubs (F1). The controllable ntptime and
the gmtime this suite needs now live in stubs.py, so it no longer bolts on
its own; it only overrides the board to set utcOffset per case.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, HERE)
import stubs

clock = stubs.clock
Requests = stubs.Requests
WLAN = stubs.WLAN
Ntptime = stubs.Ntptime

results = []


def check(label, got, expected):
    ok = got == expected
    results.append(ok)
    print('%-4s %-52s got=%r' % ('ok' if ok else 'FAIL', label, got))


# A wall-clock epoch, expressed in the Unix epoch for readability, then
# shifted into the MicroPython epoch the firmware and ntptime both use.
# 2024-06-01 12:00:00 UTC.
EPOCH_2000_OFFSET = 946684800
WALL_UNIX = 1717243200
WALL_MP = WALL_UNIX - EPOCH_2000_OFFSET


def fresh(offset=0):
    """Import main fresh in a clean directory with a chosen utcOffset.

    boot() is not run (the __name__ guard), so the clock starts unsynced and
    a test can drive sync_clock() directly. install() sets a default board,
    so the utcOffset override is applied between install and import.
    """
    os.chdir(tempfile.mkdtemp())
    Requests.raise_oserror = False
    WLAN.connected = True
    clock[0] = 0
    Ntptime.raise_exc = False
    Ntptime.next_epoch = WALL_MP
    Ntptime.calls = 0
    stubs.install()
    sys.modules['board'] = stubs._Mod(doorBellPin=16, utcOffset=offset)
    for name in stubs.FIRMWARE_MODULES:
        sys.modules.pop(name, None)
    import main
    Ntptime.calls = 0
    return main


####################################################################################
# Unsynced: the clock reports nothing until a sync lands.
####################################################################################

m = fresh()
check('clock is not live before any sync', m.clock_is_live(), False)
check('clock_now is None before any sync', m.clock_now(), None)
check('current_epoch is None before any sync', m.current_epoch(), None)
check('a ring before sync records no epoch',
      (lambda: (m.enqueue_ring(500), m.queue[0][m.Q_EPOCH])[1])(),
      None)

# log_prefix marks a relative stamp so it can never pass for an absolute one.
clock[0] = 3847221
check('log prefix is marked relative before sync',
      m.log_prefix(), 't3847221')

####################################################################################
# A good sync anchors the clock.
####################################################################################

m = fresh()
WLAN.connected = True
clock[0] = 5000
check('sync_clock reports success', m.sync_clock(), True)
check('clock is live after sync', m.clock_is_live(), True)
check('clock_now returns the synced epoch', m.clock_now(), WALL_MP)
check('epoch persisted to flash for restored-ring aging',
      m.state_get('epochAnchor', None), WALL_MP)

# The anchor plus elapsed ticks: 42 s later the clock reads 42 s on.
clock[0] = 5000 + 42000
check('clock advances with ticks', m.clock_now(), WALL_MP + 42)

# And the log prefix is now a real timestamp, not a t-marked tick count.
check('log prefix is absolute after sync',
      m.log_prefix()[:4], '2024')

####################################################################################
# format_timestamp: UTC, and a fixed local offset from board.py.
####################################################################################

# format_timestamp is pure: it needs no live clock, only the utcOffset the
# firmware was loaded with.
m = fresh(offset=0)
check('UTC timestamp formats as expected',
      m.format_timestamp(WALL_MP), '2024-06-01 12:00:00 +0000')
check('None formats as unsynced', m.format_timestamp(None), 'unsynced')

m = fresh(offset=7200)          # CEST, +02:00
check('positive offset shifts the displayed hour and tags the zone',
      m.format_timestamp(WALL_MP), '2024-06-01 14:00:00 +0200')

m = fresh(offset=-18000)        # -05:00, sign handling
check('negative offset tags a minus zone',
      m.format_timestamp(WALL_MP)[-5:], '-0500')

####################################################################################
# Bad input never poisons the clock.
####################################################################################

m = fresh()
WLAN.connected = True
Ntptime.next_epoch = 0           # a stalled read: below the sanity floor
check('an implausible epoch is rejected', m.sync_clock(), False)
check('a rejected sync leaves the clock unsynced', m.clock_is_live(), False)

m = fresh()
WLAN.connected = True
clock[0] = 5000
Ntptime.next_epoch = WALL_MP
m.sync_clock()                   # establish a good anchor first
Ntptime.next_epoch = 0           # then a bad read on the next attempt
check('good anchor stands before the bad read',
      m.clock_now(), WALL_MP)
check('a later bad read is rejected', m.sync_clock(), False)
check('the earlier good anchor survives a bad read',
      m.clock_is_live(), True)

m = fresh()
WLAN.connected = True
Ntptime.raise_exc = True
check('an NTP exception is caught, not raised', m.sync_clock(), False)
check('a raised sync leaves the clock unsynced', m.clock_is_live(), False)

####################################################################################
# Sync requires a usable link.
####################################################################################

m = fresh()
WLAN.connected = False
check('sync is skipped when WiFi is down', m.sync_clock(), False)
check('a skipped sync makes no NTP call', Ntptime.calls, 0)
WLAN.connected = True

####################################################################################
# Resync scheduling: first sync eagerly, then on the timer.
####################################################################################

m = fresh()
WLAN.connected = True
clock[0] = 1000
check('maybe_resync takes the first sync as soon as it can',
      m.maybe_resync_clock(), True)
before = Ntptime.calls
clock[0] += 1000                  # far short of NTP_RESYNC_MS
check('maybe_resync does nothing before the interval',
      m.maybe_resync_clock(), False)
check('no NTP call was made inside the interval',
      Ntptime.calls, before)
clock[0] += m.config.NTP_RESYNC_MS + 1000
check('maybe_resync resyncs once the interval passes',
      m.maybe_resync_clock(), True)

####################################################################################
# Timestamps reach the delivered ring.
####################################################################################

m = fresh()
WLAN.connected = True
clock[0] = 5000
m.sync_clock()
# A ring arriving now, with the clock live, carries its wall-clock time.
m.enqueue_ring(500)
entry = m.queue[0]
check('a live-clock ring records a real epoch', entry[m.Q_EPOCH], WALL_MP)
check('describe_delay shows the ring time',
      m.describe_delay(entry), ' (2024-06-01 12:00:00 +0000)')

# A restored ring's epoch is coarse: it is shown, but tagged approximate.
restored = [clock[0], 500, WALL_MP, True]
desc = m.describe_delay(restored)
check('a restored ring shows its approximate time',
      '2024-06-01 12:00:00 +0000' in desc and 'before a restart' in desc, True)

# With no epoch at all, the old relative wording still stands.
noepoch = [clock[0], 500, None, True]
check('a restored ring without an epoch keeps the old wording',
      m.describe_delay(noepoch), ' (queued before a restart)')

####################################################################################
# Heartbeat surfaces clock state.
####################################################################################

m = fresh()
check('heartbeat says unsynced before a sync',
      'unsynced' in m.clock_status(), True)
WLAN.connected = True
clock[0] = 5000
m.sync_clock()
check('heartbeat shows the time and a sync age after a sync',
      '2024' in m.clock_status() and 'synced' in m.clock_status(),
      True)

####################################################################################

print('\n%d/%d passed' % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
