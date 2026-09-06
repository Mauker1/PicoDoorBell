"""Verify the boot sequence configures hardware before networking.

The bug B3 fixes: connect_wifi() ran at module scope outside any try and
sent the startup message straight after association. If that send raised
-- typically DNS not ready yet -- the script died before machine.Pin(16)
was reached, leaving the doorbell dead until a power cycle.

Runs main.py under stub hardware modules. The trailing `while True:` loop
is stripped via AST, since executing it would never return.
"""
import ast
import os
import sys
import tempfile

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'main.py')

events = []
results = []


def check(label, got, expected):
    ok = got == expected
    results.append(ok)
    print('%-4s %-54s got=%r' % ('ok' if ok else 'FAIL', label, got))


# --------------------------------------------------------------------------
# Stub hardware
# --------------------------------------------------------------------------
class _Mod:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Pin:
    IN = 'IN'
    OUT = 'OUT'
    PULL_DOWN = 'PULL_DOWN'
    IRQ_RISING = 1
    IRQ_FALLING = 2

    def __init__(self, ident, mode=None, pull=None):
        self.ident = ident
        self.handler = None
        self._level = 0
        events.append('pin_%s' % ident)

    def irq(self, handler=None, trigger=None):
        self.handler = handler
        self.trigger = trigger

    def on(self):
        pass

    def off(self):
        pass

    def value(self, level=None):
        if level is None:
            return self._level
        self._level = level

    def edge(self, level):
        """Drive the pin and fire its interrupt, as the hardware would."""
        self._level = level
        if self.handler is not None:
            self.handler(self)


class WLAN:
    connected = True
    fail_connect = False

    def __init__(self, iface=None):
        pass

    def active(self, on):
        events.append('wlan_active')

    def status(self):
        return 3 if WLAN.connected else 0

    def connect(self, ssid, pw):
        events.append('wlan_connect')

    def ifconfig(self):
        return ('192.0.2.10', '255.255.255.0', '192.0.2.1', '192.0.2.1')

    def config(self, key):
        return b'\xde\xad\xbe\xef\x00\x01'


class WDT:
    """Records arming and feeding; never actually bites."""
    instances = []
    available = True

    def __init__(self, timeout=None):
        if not WDT.available:
            raise ValueError('WDT unavailable')
        self.timeout = timeout
        self.feeds = 0
        WDT.instances.append(self)
        events.append('wdt_armed')

    def feed(self):
        self.feeds += 1
        events.append('wdt_fed')


class Mem32:
    """Emulates machine.mem32 as a sparse address space."""

    def __init__(self):
        self.cells = {}

    def __getitem__(self, addr):
        return self.cells.get(addr, 0)

    def __setitem__(self, addr, value):
        self.cells[addr] = value & 0xFFFFFFFF


mem32 = Mem32()


class Response:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {'ok': True, 'result': []}

    def json(self):
        return self._payload

    def close(self):
        events.append('http_close')


class Requests:
    raise_oserror = False

    def post(self, url, json=None):
        events.append('http_post')
        if Requests.raise_oserror:
            raise OSError(-2, 'DNS lookup failed')
        return Response()

    def get(self, url):
        events.append('http_get')
        if Requests.raise_oserror:
            raise OSError(-2, 'DNS lookup failed')
        return Response()


machine_reset_cause = [1]   # PWRON_RESET by default


clock = [0]


def _ticks_ms():
    return clock[0]


def _ticks_diff(a, b):
    return a - b


def _ticks_add(a, b):
    return a + b


def install_stubs():
    sys.modules['rp2'] = _Mod(country=lambda c: events.append('country'))
    sys.modules['network'] = _Mod(WLAN=WLAN, STA_IF='STA_IF')
    sys.modules['machine'] = _Mod(Pin=Pin, mem32=mem32, WDT=WDT,
                                  reset_cause=lambda: machine_reset_cause[0],
                                  PWRON_RESET=1, HARD_RESET=2, WDT_RESET=3,
                                  DEEPSLEEP_RESET=4, SOFT_RESET=5)
    sys.modules['ubinascii'] = _Mod(
        hexlify=lambda b, sep=None: b':'.join(b'%02x' % c for c in b))
    sys.modules['urequests'] = Requests()
    sys.modules['board'] = _Mod(doorBellPin=16)
    sys.modules['secrets'] = _Mod(secrets={
        'ssid': 'net', 'pw': 'pw', 'botToken': 'TOKEN', 'telegramDmUid': '-1001',
    })
    sys.modules['time'] = _Mod(sleep=lambda s: None,
                               sleep_ms=lambda ms: None,
                               ticks_ms=_ticks_ms,
                               ticks_diff=_ticks_diff, ticks_add=_ticks_add)


def load_firmware():
    """Exec main.py with the main loop removed."""
    tree = ast.parse(open(SRC).read())
    tree.body = [n for n in tree.body if not isinstance(n, ast.While)]
    ns = {'__name__': 'main'}
    exec(compile(tree, 'main.py', 'exec'), ns)
    return ns


work = tempfile.mkdtemp()
os.chdir(work)
install_stubs()

# --- 1. Happy path: hardware precedes networking --------------------------
del events[:]
Requests.raise_oserror = False
ns = load_firmware()

check('doorbell pin was configured', ns['doorBellInput'] is not None, True)
check('watchdog armed during boot', 'wdt_armed' in events, True)
check('watchdog armed after the pin, before networking',
      events.index('pin_16') < events.index('wdt_armed') < events.index('http_post'),
      True)
check('timeout stays under the RP2040 ceiling',
      ns['WDT_TIMEOUT_MS'] < 8300, True)
check('LED was configured', ns['led'] is not None, True)
pin_at = events.index('pin_16')
net_at = events.index('http_post')
check('pin configured before any network call', pin_at < net_at, True)
check('wlan activated before LED created',
      events.index('wlan_active') < events.index('pin_LED'), True)
check('startup message was attempted', 'http_post' in events, True)
check('reset info captured at boot', ns['resetInfo'] is not None, True)
check('boot number derives from flash', ns['bootNumber'], 1)
check('nothing persisted before the device proves stable',
      os.path.exists('state.json'), False)
check('isStartup cleared after announcing', ns['isStartup'], False)

# --- 2. The actual B3 regression: startup send fails -----------------------
del events[:]
Requests.raise_oserror = True
ns = load_firmware()

check('boot survived a failing startup send', ns['doorBellInput'] is not None, True)
check('LED still configured after send failure', ns['led'] is not None, True)
check('send was genuinely attempted', 'http_post' in events, True)
check('pin still precedes the failed call',
      events.index('pin_16') < events.index('http_post'), True)

# --- 3. connect_wifi must not notify --------------------------------------
del events[:]
Requests.raise_oserror = False
ns = load_firmware()
del events[:]
ns['connect_wifi']()
check('connect_wifi sends nothing', 'http_post' in events, False)

# --- 4. announce_startup picks the right message ---------------------------
sent = []
ns['send_message'] = lambda chat, msg: (sent.append(msg), (0, 200, {}))[1]
ns['isStartup'] = True
ns['announce_startup']()
ns['announce_startup']()
check('first announce carries the startup text',
      sent[0].startswith(ns['startupText']), True)
check('second announce carries the reconnect text',
      sent[1].startswith(ns['reconnectText']), True)
check('announcements carry the reset summary', 'Reset: boot #' in sent[0], True)

# --- 5. Board definition is mandatory --------------------------------------
saved_board = sys.modules.pop('board')
try:
    load_firmware()
    check('missing board.py refuses to run', False, True)
except ImportError as e:
    check('missing board.py refuses to run', 'board.py' in str(e), True)
finally:
    sys.modules['board'] = saved_board

sys.modules['board'] = _Mod()          # present but empty
try:
    load_firmware()
    check('incomplete board.py refuses to run', False, True)
except ValueError as e:
    check('incomplete board.py names the missing pin', 'doorBellPin' in str(e), True)
finally:
    sys.modules['board'] = saved_board

sys.modules['board'] = _Mod(doorBellPin=18)
del events[:]
ns = load_firmware()
check('board.py drives the pin number', 'pin_18' in events, True)
check('no hardcoded pin fallback', 'pin_16' in events, False)
sys.modules['board'] = saved_board

# --- 6. Socket released even when the send fails ---------------------------
del events[:]
Requests.raise_oserror = False
ns = load_firmware()
check('every request was closed',
      events.count('http_close'), events.count('http_post') + events.count('http_get'))

print()
print('%d/%d passed' % (sum(results), len(results)))
raise SystemExit(0 if all(results) else 1)
