"""Verify B2: the watchdog is armed, and fed everywhere it needs to be.

The RP2040 watchdog tops out near 8.3 s, which is close to what one loop
pass can legitimately take: a Telegram round trip is 1-2 s measured, and a
worst-case pass can hold a send, a getUpdates and a flash sector erase. The
margin comes from feeding inside the blocking work rather than only at the
top of the loop.

The specific regression guarded here: any sleep longer than the timeout
resets the board. The 10 s grace period after an error was exactly that.

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

events = _stubs['events']
mem32 = _stubs['mem32']
Requests = _stubs['Requests']
WDT = _stubs['WDT']

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


def fresh():
    del events[:]
    del WDT.instances[:]
    mem32.cells.clear()
    return load_firmware()


os.chdir(tempfile.mkdtemp())
Requests.raise_oserror = False

# --- 1. Armed once, with a safe timeout ------------------------------------
ns = fresh()
check('exactly one watchdog created', len(WDT.instances), 1)
check('timeout below the 8.3 s ceiling', WDT.instances[0].timeout < 8300, True)
check('timeout leaves room for two round trips',
      WDT.instances[0].timeout >= 5000, True)
check('arming twice is a no-op', (ns['arm_watchdog'](), len(WDT.instances))[1], 1)

# --- 2. Fed around network work --------------------------------------------
# A handshake can run for seconds; the bite must not land mid-request.
ns = fresh()
before = WDT.instances[0].feeds
ns['do_request']('GET', 'https://example.invalid/x')
check('a request feeds the watchdog', WDT.instances[0].feeds > before, True)

# Even a failing request must feed, or a flapping network kills the board.
Requests.raise_oserror = True
before = WDT.instances[0].feeds
ns['do_request']('GET', 'https://example.invalid/x')
check('a failed request still feeds', WDT.instances[0].feeds > before, True)
Requests.raise_oserror = False

# --- 3. The regression: long sleeps are sliced -----------------------------
# sleep(10) left whole would outlast an 8 s timeout and reset the board on
# every error, turning a transient fault into a reboot loop.
ns = fresh()
before = WDT.instances[0].feeds
ns['sleep_fed'](10)
feeds = WDT.instances[0].feeds - before
check('a 10 s wait feeds many times', feeds >= 20, True)
check('feeds are spaced under the timeout',
      (10000 / feeds) < ns['WDT_TIMEOUT_MS'], True)

before = WDT.instances[0].feeds
ns['sleep_fed'](0.1)
check('a short wait still feeds', WDT.instances[0].feeds > before, True)

before = WDT.instances[0].feeds
ns['sleep_fed'](0)
check('a zero wait feeds once', WDT.instances[0].feeds - before, 1)

# --- 4. No unfed sleep long enough to bite ---------------------------------
# Static check: every time.sleep() left in the source must be short, since
# only sleep_fed() feeds. blink_onboard_led and error_halt are the survivors.
tree = ast.parse(open(SRC).read())
long_sleeps = []
for node in ast.walk(tree):
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'sleep'
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == 'time'):
        arg = node.args[0] if node.args else None
        if isinstance(arg, ast.Constant) and isinstance(arg.value, (int, float)):
            if arg.value * 1000 >= 8000:
                long_sleeps.append((node.lineno, arg.value))
        elif not isinstance(arg, ast.Constant):
            long_sleeps.append((node.lineno, 'non-constant'))
check('no raw sleep can outlast the watchdog', long_sleeps, [])

# --- 4b. A dropped network must not become a reset -------------------------
# Observed on hardware, twice: WiFi vanished while a request was in flight,
# urequests blocked with no timeout, and the watchdog reset the board. Once
# during a send (losing the ring) and once during getUpdates.
ns = fresh()
WLAN = _stubs['WLAN']

WLAN.connected = False
try:
    del events[:]
    outcome, status, body = ns['do_request']('GET', 'https://example.invalid/x')
    check('no socket is opened while WiFi is down', 'http_get' in events, False)
    check('it reports the request as skipped, not failed',
          outcome, ns['REQUEST_SKIPPED'])
finally:
    WLAN.connected = True

# A timeout below the watchdog turns a stalled socket into an outcome.
Requests.last_timeout = None
ns['do_request']('GET', 'https://example.invalid/x')
check('requests carry a timeout', Requests.last_timeout, ns['REQUEST_TIMEOUT_S'])
check('the timeout fires before the watchdog',
      ns['REQUEST_TIMEOUT_S'] * 1000 < ns['WDT_TIMEOUT_MS'], True)

# An older urequests without the parameter must still work.
Requests.accepts_timeout = False
try:
    ns = fresh()
    del events[:]
    outcome, status, body = ns['do_request']('GET', 'https://example.invalid/x')
    check('falls back when timeout is unsupported', outcome, ns['REQUEST_OK'])
    check('the request still happened', 'http_get' in events, True)
    check('and it stops trying', ns['requestsTimeoutSupported'], False)
finally:
    Requests.accepts_timeout = True

# --- 4c. Reconnection must not hammer the driver ---------------------------
# A ten-minute outage previously issued ~200 connect() calls at three-second
# intervals. Afterwards the board associated -- status 3, IP printed -- but
# passed no traffic. Re-issuing while an attempt is in flight wedges the
# cyw43 stack.
ns = fresh()
WLAN = _stubs['WLAN']
clock = _stubs['clock']

WLAN.connected = False
WLAN.connect_after = 3          # succeeds on the third attempt
del events[:]
clock[0] = 0


# sleep_fed does not advance the stub clock, so drive it from the poll.
real_sleep_fed = ns['sleep_fed']


def ticking_sleep(seconds):
    clock[0] += int(seconds * 1000)
    real_sleep_fed(seconds)


ns['sleep_fed'] = ticking_sleep
ns['connect_wifi']()
issued = events.count('wlan_connect')
check('one connect per attempt, not per poll', issued, 3)
check('it did connect in the end', WLAN.connected, True)
check('no reset was needed', 'machine_reset' in events, False)

# Give up and reset rather than flail forever. Boot with WiFi up, since
# boot() itself calls connect_wifi() and would otherwise loop here.
WLAN.connected = True
ns2 = fresh()
ns2['sleep_fed'] = ticking_sleep
WLAN.connected = False
WLAN.connect_after = None
del events[:]
try:
    ns2['connect_wifi']()
except _stubs['ResetCalled']:
    pass
check('a hopeless outage ends in a reset', 'machine_reset' in events, True)
check('attempts are bounded',
      events.count('wlan_connect') <= ns2['WIFI_MAX_ATTEMPTS'], True)
check('the reset is marked as self-inflicted',
      mem32[0x40058000 + 0x0c + 4], ns2['SCRATCH_INTENT'])
WLAN.connected = True

# --- 4d. An associated but dead stack must be detected ---------------------
# Reproduced on the bench: after the AP rejected the board for a few minutes
# it re-associated -- status 3, IP printed -- but every DNS lookup returned
# -2, indefinitely. B4's counter does not help; it only guards the
# connection phase, and once status reads 3 the reset path is unreachable.
WLAN.connected = True
ns = fresh()
ns['sleep_fed'] = lambda s: None
Requests.raise_oserror = True
del events[:]

ns['lastNetworkSuccess'] = -ns['NETWORK_DEAD_MS'] - 1000
for _ in range(3):
    # Backoff would otherwise skip most of these without attempting.
    ns['networkBackoffMs'] = 0
    ns['do_request']('GET', 'https://example.invalid/x')
check('the first remedy is a bounce, not a reset',
      ns['networkBounceRequested'], True)
check('nothing was reset yet', 'machine_reset' in events, False)

# The bounce keeps the log, the queue and the uptime.
lines_before = len(ns['logLines'])
ns['sleep_fed'] = lambda s: None
ns['bounce_wifi']()
check('the bounce clears the request', ns['networkBounceRequested'], False)
check('and records that it was tried', ns['networkBounced'], True)
check('the log survives a bounce', len(ns['logLines']) >= lines_before, True)

# Still failing after a bounce: now a reset is the only local action left.
try:
    ns['lastNetworkSuccess'] = -ns['NETWORK_DEAD_MS'] - 1000
    for _ in range(3):
        ns['networkBackoffMs'] = 0
        ns['do_request']('GET', 'https://example.invalid/x')
    check('failures after a bounce end in a reset',
          'machine_reset' in events, True)
except _stubs['ResetCalled']:
    check('failures after a bounce end in a reset', True, True)
Requests.raise_oserror = False

# Recovery clears the escalation, so the next outage starts from a bounce.
ns = fresh()
ns['note_request_result'](ns['REQUEST_RETRY'])
ns['networkBounced'] = True
ns['note_request_result'](ns['REQUEST_OK'])
check('recovery resets the escalation', ns['networkBounced'], False)

# The trigger is elapsed time, not a failure count. Backoff stretches a count
# into an unpredictable duration: fifteen failures worked out at about twelve
# minutes, by which point the AP had deauthenticated the board and this path
# was unreachable.
ns = fresh()
Requests.raise_oserror = True
ns['lastNetworkSuccess'] = 0
clock[0] = ns['NETWORK_DEAD_MS'] // 2
ns['networkBackoffMs'] = 0
ns['do_request']('GET', 'https://example.invalid/x')
check('a short dead spell asks for nothing',
      ns['networkBounceRequested'], False)
clock[0] = ns['NETWORK_DEAD_MS'] + 1000
ns['networkBackoffMs'] = 0
ns['do_request']('GET', 'https://example.invalid/x')
check('a long one asks for a bounce', ns['networkBounceRequested'], True)
Requests.raise_oserror = False

# A reset reason survives the reset it describes, since the log does not.
ns = fresh()
try:
    ns['self_reset']('testing', ns['REASON_WIFI'])
except _stubs['ResetCalled']:
    pass
check('the reason is left in scratch 0',
      mem32[0x40058000 + 0x0c], ns['REASON_WIFI'])

info = ns['read_reset_info']()
check('and is read back on the next boot',
      info['resetReason'], 'WiFi unreachable')
check('then cleared', mem32[0x40058000 + 0x0c], 0)
check('and it reaches the summary',
      'reason=WiFi_unreachable' in ns['format_reset_info'](info), True)

# Failures back off instead of retrying every pass.
ns = fresh()
Requests.raise_oserror = True
ns['note_request_result'](ns['REQUEST_RETRY'])
first = ns['networkBackoffMs']
ns['note_request_result'](ns['REQUEST_RETRY'])
second = ns['networkBackoffMs']
check('backoff grows', second > first, True)
check('backoff is capped',
      ns['NETWORK_BACKOFF_MAX_MS'] >= second, True)

del events[:]
outcome, status, body = ns['do_request']('GET', 'https://example.invalid/x')
check('a backed-off request is skipped, not attempted',
      'http_get' in events, False)
# A skip is not evidence of anything: it must not count toward the
# dead-network deadline, and must not be logged as a failure.
ns['lastNetworkSuccess'] = -ns['NETWORK_DEAD_MS'] - 1000
ns['networkBounceRequested'] = False
outcome, status, body = ns['do_request']('GET', 'https://example.invalid/x')
check('a skipped request reports as skipped', outcome, ns['REQUEST_SKIPPED'])
check('and does not trigger the escalation',
      ns['networkBounceRequested'], False)
Requests.raise_oserror = False

# A Telegram error is not a transport failure and must not count.
ns = fresh()
before = ns['networkFailures']
ns['note_request_result'](ns['REQUEST_FATAL'])
check('a 4xx does not count as a dead stack',
      ns['networkFailures'], before)

# Success clears the counter.
ns = fresh()
ns['note_request_result'](ns['REQUEST_RETRY'])
ns['note_request_result'](ns['REQUEST_OK'])
check('success resets the failure count', ns['networkFailures'], 0)
check('and clears the backoff', ns['networkBackoffMs'], 0)

# --- 4e. A join in progress must not be interrupted ------------------------
# Observed on the bench: seven attempts over three and a half minutes on a
# network that was perfectly available. Status 2 means associated and
# awaiting DHCP; calling connect() again aborts that and starts over.
WLAN.connected = True
ns = fresh()
ns['sleep_fed'] = ticking_sleep
WLAN.connected = False
WLAN.status_code = 2          # joining, making progress
WLAN.connect_after = None
del events[:]
clock[0] = 0

# While the status reports progress, connect() must be re-issued on the
# longer allowance, not the stalled-attempt interval.
WLAN.connect_after = 200      # never succeeds within the window under test
check('progress states are recognised',
      2 in ns['WIFI_PROGRESS_STATES'], True)
check('a stalled attempt retries sooner than a progressing one',
      ns['WIFI_REISSUE_MS'] < ns['WIFI_PROGRESS_MAX_MS'], True)
check('but not so soon that it hammers the driver',
      ns['WIFI_REISSUE_MS'] >= 5000, True)
check('status 2 is named, not printed raw',
      ns['wifi_status_name'](2), 'awaiting IP')
check('-3 is not called a wrong password',
      ns['wifi_status_name'](-3), 'auth rejected')

# A terminal status does retry at the shorter interval.
WLAN.status_code = -2         # no AP found
WLAN.connect_after = 3
del events[:]
clock[0] = 0
ns['connect_wifi']()
check('a terminal status still retries', events.count('wlan_connect'), 3)
WLAN.status_code = 0
WLAN.connected = True

# --- 5. The flash write is covered -----------------------------------------
# A sector erase stalls the CPU with interrupts disabled.
ns = fresh()
ns['bootStableAt'] = -1
before = WDT.instances[0].feeds
ns['mark_boot_stable']()
check('the boot write feeds afterwards', WDT.instances[0].feeds > before, True)

# --- 6. A missing watchdog must not stop the device ------------------------
# An unguarded device still answers the door; one that refuses to boot does not.
WDT.available = False
try:
    ns = fresh()
    check('boot survives an unavailable watchdog',
          ns['doorBellInput'] is not None, True)
    check('wdt stays None', ns['wdt'], None)
    check('feeding a missing watchdog is harmless',
          (ns['feed_watchdog'](), True)[1], True)
    check('sleep_fed still works without one',
          (ns['sleep_fed'](0.1), True)[1], True)
finally:
    WDT.available = True

print()
print('%d/%d passed' % (sum(results), len(results)))
raise SystemExit(0 if all(results) else 1)
