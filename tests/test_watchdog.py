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
    check('it reports a retryable outcome', outcome, ns['REQUEST_RETRY'])
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
