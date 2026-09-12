"""Verify the boot sequence configures hardware before networking.

The bug B3 fixes: connect_wifi() ran at module scope outside any try and
sent the startup message straight after association. If that send raised
(typically DNS not ready yet) the script died before machine.Pin(16) was
reached, leaving the doorbell dead until a power cycle.

Imports main under the host-side stubs (F1) and runs boot() explicitly:
boot() no longer runs at import, since the __name__ guard holds it back.
The board-guard cases below deliberately import without booting, because
they assert that the import itself refuses a missing or incomplete board.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, HERE)
import stubs

events = stubs.events
Requests = stubs.Requests
WLAN = stubs.WLAN
_Mod = stubs._Mod

results = []


def check(label, got, expected):
    ok = got == expected
    results.append(ok)
    print('%-4s %-54s got=%r' % ('ok' if ok else 'FAIL', label, got))


def load_and_boot():
    """Import main fresh and run boot(), producing the boot event trace."""
    WLAN.connected = True
    m = stubs.load_firmware()
    m.boot()
    return m


work = tempfile.mkdtemp()
os.chdir(work)

# --- 1. Happy path: hardware precedes networking --------------------------
del events[:]
Requests.raise_oserror = False
m = load_and_boot()

check('doorbell pin was configured', m.doorBellInput is not None, True)
check('watchdog armed during boot', 'wdt_armed' in events, True)
check('watchdog armed after the pin, before networking',
      events.index('pin_16') < events.index('wdt_armed') < events.index('http_post'),
      True)
check('timeout stays under the RP2040 ceiling',
      m.config.WDT_TIMEOUT_MS < 8300, True)
check('LED was configured', m.led is not None, True)
check('power save was disabled', WLAN.pm_set, m.WIFI_PM_NONE)
check('and set before connecting',
      events.index('wlan_active') < events.index('http_get'), True)
pin_at = events.index('pin_16')
net_at = events.index('http_post')
check('pin configured before any network call', pin_at < net_at, True)
check('wlan activated before LED created',
      events.index('wlan_active') < events.index('pin_LED'), True)
check('startup message was attempted', 'http_post' in events, True)
check('reset info captured at boot', m.resetInfo is not None, True)
check('the update backlog is discarded before announcing',
      events.index('http_get') < events.index('http_post'), True)
check('boot number derives from flash', m.bootNumber, 1)
check('nothing persisted before the device proves stable',
      os.path.exists('state.json'), False)
check('isStartup cleared after announcing', m.isStartup, False)

# --- 2. The actual B3 regression: startup send fails -----------------------
del events[:]
Requests.raise_oserror = True
m = load_and_boot()

check('boot survived a failing startup send', m.doorBellInput is not None, True)
check('LED still configured after send failure', m.led is not None, True)
check('the announcement is held, not lost',
      m.pendingAnnouncement is not None, True)
check('pin still precedes the first network call',
      events.index('pin_16') < events.index('http_get'), True)

# --- 3. connect_wifi must not notify --------------------------------------
del events[:]
Requests.raise_oserror = False
m = load_and_boot()
del events[:]
m.connect_wifi()
check('connect_wifi sends nothing', 'http_post' in events, False)

# --- 4. announce_startup picks the right message ---------------------------
sent = []
m.send_message = lambda chat, msg: (sent.append(msg), (0, 200, {}))[1]
m.isStartup = True
m.announce_startup()
m.announce_startup()
check('first announce carries the startup text',
      sent[0].startswith(m.config.startupText), True)
check('second announce carries the reconnect text',
      sent[1].startswith(m.config.reconnectText), True)
check('the startup message carries the reset summary',
      'Reset: boot #' in sent[0], True)
check('the reconnect message does not',
      'Reset: boot #' in sent[1], False)

# --- 5. Board definition is mandatory --------------------------------------
# These assert the import itself refuses a bad board, so they import without
# booting. install() first, to reset every other module, then override board.
stubs.install()
saved_board = sys.modules.pop('board')
try:
    sys.modules.pop('main', None)
    import main
    check('missing board.py refuses to run', False, True)
except ImportError as e:
    check('missing board.py refuses to run', 'board.py' in str(e), True)
finally:
    sys.modules['board'] = saved_board

sys.modules['board'] = _Mod()          # present but empty
try:
    sys.modules.pop('main', None)
    import main
    check('incomplete board.py refuses to run', False, True)
except ValueError as e:
    check('incomplete board.py names the missing pin', 'doorBellPin' in str(e), True)
finally:
    sys.modules['board'] = saved_board

stubs.install()
sys.modules['board'] = _Mod(doorBellPin=18)
del events[:]
sys.modules.pop('main', None)
import main
main.boot()
check('board.py drives the pin number', 'pin_18' in events, True)
check('no hardcoded pin fallback', 'pin_16' in events, False)

# --- 6. Socket released even when the send fails ---------------------------
del events[:]
Requests.raise_oserror = False
m = load_and_boot()
check('every request was closed',
      events.count('http_close'), events.count('http_post') + events.count('http_get'))

print()
print('%d/%d passed' % (sum(results), len(results)))
raise SystemExit(0 if all(results) else 1)
