"""Verify C1: the NTP wall clock, its fallback, and timestamped output.

The feature exists so that a log line and a queued ring can say *when*,
not merely how long since boot. ticks_ms wraps at ~12.4 days and means
nothing in an incident report; the reboot investigation on the production
unit is easier to correlate against real times than against relative ones.

The design is an anchor, not repeated polling: one NTP read captures the
epoch at a known ticks_ms, and every later time is anchor + elapsed. These
tests drive that anchor through its states: never synced, freshly synced,
advancing, resynced, and fed deliberately bad input.

Reuses the stub hardware from test_boot.py, with two additions this suite
needs and the base does not: a real time.gmtime (the base time stub has
only the ticks_* family) and a controllable ntptime module (the firmware
treats an absent one as "never syncs", which is itself tested here).
"""
import ast
import os
import sys
import tempfile
import time as _real_time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', 'main.py')

# Reuse the stubs without running test_boot's own assertions.
_src = open(os.path.join(HERE, 'test_boot.py')).read()
_stubs = {'__name__': 'stubs', '__file__': os.path.join(HERE, 'test_boot.py')}
exec(compile(_src[:_src.index('work = tempfile.mkdtemp()')], 'stubs', 'exec'), _stubs)
_stubs['install_stubs']()

clock = _stubs['clock']
Requests = _stubs['Requests']
WLAN = _stubs['WLAN']

results = []


def check(label, got, expected):
    ok = got == expected
    results.append(ok)
    print('%-4s %-52s got=%r' % ('ok' if ok else 'FAIL', label, got))


####################################################################################
# Stub extensions for this suite.
####################################################################################

# The base time stub has no gmtime; format_timestamp needs one. MicroPython's
# gmtime uses the 2000 epoch, and so does the firmware's arithmetic, but the
# host's gmtime uses the 1970 epoch. Convert at the boundary so the test's
# expected strings can be written against ordinary Unix times.
EPOCH_2000_OFFSET = 946684800


def _gmtime(secs):
    return _real_time.gmtime(secs + EPOCH_2000_OFFSET)


_stubs['sys'].modules['time'].gmtime = _gmtime


class _NtpTime:
    """Controllable ntptime stub.

    next_epoch is what time() returns (in the MicroPython 2000 epoch, as the
    real ntptime does). Setting raise_exc makes time() raise, standing in for
    a lost UDP packet or a dead server.
    """
    next_epoch = 0
    raise_exc = False
    timeout = None
    calls = 0

    @staticmethod
    def time():
        _NtpTime.calls += 1
        if _NtpTime.raise_exc:
            raise OSError('simulated NTP failure')
        return _NtpTime.next_epoch


ntp = _NtpTime()
sys.modules['ntptime'] = ntp


def load_firmware():
    tree = ast.parse(open(SRC).read())
    tree.body = [n for n in tree.body if not isinstance(n, ast.While)]
    ns = {'__name__': 'main'}
    exec(compile(tree, 'main.py', 'exec'), ns)
    return ns


# A wall-clock epoch, expressed in the Unix epoch for readability, then
# shifted into the MicroPython epoch the firmware and ntptime both use.
# 2024-06-01 12:00:00 UTC.
WALL_UNIX = 1717243200
WALL_MP = WALL_UNIX - EPOCH_2000_OFFSET


def fresh(offset=0):
    """Load the firmware in a clean directory with a chosen utcOffset.

    Boots with WiFi up, matching test_boot: connect_wifi() would otherwise
    spin, since the stub link never comes up on its own. boot() therefore
    syncs the clock during load. Tests that need an *unsynced* clock call
    unsync(ns) to return it to its just-booted-with-no-sync state; the
    firmware's own globals are reset directly, which is what that state is.
    """
    os.chdir(tempfile.mkdtemp())
    Requests.raise_oserror = False
    WLAN.connected = True
    clock[0] = 0
    _NtpTime.raise_exc = False
    _NtpTime.next_epoch = WALL_MP
    _NtpTime.calls = 0
    saved = sys.modules['board']
    sys.modules['board'] = _stubs['_Mod'](doorBellPin=16, utcOffset=offset)
    try:
        ns = load_firmware()
    finally:
        sys.modules['board'] = saved
    _NtpTime.calls = 0
    return ns


def unsync(ns):
    """Return a loaded firmware's clock to the never-synced state.

    boot() syncs during load, so an unsynced clock cannot be observed
    straight from fresh(). Clearing the anchor and the sync flags is exactly
    what "never synced" is: the same values the module holds at import,
    before boot() runs. The persisted epochAnchor is cleared too, so
    clock_now() cannot fall back to it.
    """
    ns['clockAnchorEpoch'] = None
    ns['clockAnchorTicks'] = 0
    ns['clockEverSynced'] = False
    ns['lastNtpSync'] = 0
    ns['state']['epochAnchor'] = None
    _NtpTime.calls = 0


def synced(offset=0):
    """A freshly loaded firmware with one good sync (boot did it)."""
    return fresh(offset)


####################################################################################
# Unsynced: the clock reports nothing until a sync lands.
####################################################################################

ns = fresh()
unsync(ns)
check('clock is not live before any sync', ns['clock_is_live'](), False)
check('clock_now is None before any sync', ns['clock_now'](), None)
check('current_epoch is None before any sync', ns['current_epoch'](), None)
check('a ring before sync records no epoch',
      (lambda: (ns['enqueue_ring'](500), ns['queue'][0][ns['Q_EPOCH']])[1])(),
      None)

# log_prefix marks a relative stamp so it can never pass for an absolute one.
clock[0] = 3847221
check('log prefix is marked relative before sync',
      ns['log_prefix'](), 't3847221')

####################################################################################
# A good sync anchors the clock.
####################################################################################

ns = fresh()
unsync(ns)
WLAN.connected = True
clock[0] = 5000
check('sync_clock reports success', ns['sync_clock'](), True)
check('clock is live after sync', ns['clock_is_live'](), True)
check('clock_now returns the synced epoch', ns['clock_now'](), WALL_MP)
check('epoch persisted to flash for restored-ring aging',
      ns['state_get']('epochAnchor', None), WALL_MP)

# The anchor plus elapsed ticks: 42 s later the clock reads 42 s on.
clock[0] = 5000 + 42000
check('clock advances with ticks', ns['clock_now'](), WALL_MP + 42)

# And the log prefix is now a real timestamp, not a t-marked tick count.
check('log prefix is absolute after sync',
      ns['log_prefix']()[:4], '2024')

####################################################################################
# format_timestamp: UTC, and a fixed local offset from board.py.
####################################################################################

# format_timestamp is pure: it needs no live clock, only the utcOffset the
# firmware was loaded with.
ns = fresh(offset=0)
check('UTC timestamp formats as expected',
      ns['format_timestamp'](WALL_MP), '2024-06-01 12:00:00 +0000')
check('None formats as unsynced', ns['format_timestamp'](None), 'unsynced')

ns = fresh(offset=7200)          # CEST, +02:00
check('positive offset shifts the displayed hour and tags the zone',
      ns['format_timestamp'](WALL_MP), '2024-06-01 14:00:00 +0200')

ns = fresh(offset=-18000)        # -05:00, sign handling
check('negative offset tags a minus zone',
      ns['format_timestamp'](WALL_MP)[-5:], '-0500')

####################################################################################
# Bad input never poisons the clock.
####################################################################################

ns = fresh()
unsync(ns)
WLAN.connected = True
_NtpTime.next_epoch = 0           # a stalled read: below the sanity floor
check('an implausible epoch is rejected', ns['sync_clock'](), False)
check('a rejected sync leaves the clock unsynced', ns['clock_is_live'](), False)

ns = fresh()
WLAN.connected = True
clock[0] = 5000
_NtpTime.next_epoch = WALL_MP
ns['sync_clock']()                # establish a good anchor first
_NtpTime.next_epoch = 0           # then a bad read on the next attempt
check('good anchor stands before the bad read',
      ns['clock_now'](), WALL_MP)
check('a later bad read is rejected', ns['sync_clock'](), False)
check('the earlier good anchor survives a bad read',
      ns['clock_is_live'](), True)

ns = fresh()
unsync(ns)
WLAN.connected = True
_NtpTime.raise_exc = True
check('an NTP exception is caught, not raised', ns['sync_clock'](), False)
check('a raised sync leaves the clock unsynced', ns['clock_is_live'](), False)

####################################################################################
# Sync requires a usable link.
####################################################################################

ns = fresh()
unsync(ns)
WLAN.connected = False
check('sync is skipped when WiFi is down', ns['sync_clock'](), False)
check('a skipped sync makes no NTP call', _NtpTime.calls, 0)
WLAN.connected = True

####################################################################################
# Resync scheduling: first sync eagerly, then on the timer.
####################################################################################

ns = fresh()
unsync(ns)
WLAN.connected = True
clock[0] = 1000
check('maybe_resync takes the first sync as soon as it can',
      ns['maybe_resync_clock'](), True)
before = _NtpTime.calls
clock[0] += 1000                  # far short of NTP_RESYNC_MS
check('maybe_resync does nothing before the interval',
      ns['maybe_resync_clock'](), False)
check('no NTP call was made inside the interval',
      _NtpTime.calls, before)
clock[0] += ns['NTP_RESYNC_MS'] + 1000
check('maybe_resync resyncs once the interval passes',
      ns['maybe_resync_clock'](), True)

####################################################################################
# Timestamps reach the delivered ring.
####################################################################################

ns = synced()
# A ring arriving now, with the clock live, carries its wall-clock time.
ns['enqueue_ring'](500)
entry = ns['queue'][0]
check('a live-clock ring records a real epoch', entry[ns['Q_EPOCH']], WALL_MP)
check('describe_delay shows the ring time',
      ns['describe_delay'](entry), ' (2024-06-01 12:00:00 +0000)')

# A restored ring's epoch is coarse: it is shown, but tagged approximate.
restored = [clock[0], 500, WALL_MP, True]
desc = ns['describe_delay'](restored)
check('a restored ring shows its approximate time',
      '2024-06-01 12:00:00 +0000' in desc and 'before a restart' in desc, True)

# With no epoch at all, the old relative wording still stands.
noepoch = [clock[0], 500, None, True]
check('a restored ring without an epoch keeps the old wording',
      ns['describe_delay'](noepoch), ' (queued before a restart)')

####################################################################################
# Heartbeat surfaces clock state.
####################################################################################

ns = fresh()
unsync(ns)
check('heartbeat says unsynced before a sync',
      'unsynced' in ns['clock_status'](), True)
WLAN.connected = True
clock[0] = 5000
ns['sync_clock']()
check('heartbeat shows the time and a sync age after a sync',
      '2024' in ns['clock_status']() and 'synced' in ns['clock_status'](),
      True)

####################################################################################

print('\n%d/%d passed' % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
