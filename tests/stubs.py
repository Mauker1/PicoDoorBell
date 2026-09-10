"""Host-side fakes for the hardware modules main.py imports (F1).

main.py imports machine, network, rp2, urequests, ubinascii, time, gc, board
and secrets, none of which exist under CPython. This module provides stand-ins
faithful enough that the firmware's logic runs on a host, so the suites can
`import main` (see the __name__ guard at the foot of main.py) instead of
AST-stripping the loop and exec-ing the source.

install() must run before `import main`, since the import binds these modules
at the top of the file. load_firmware() wraps the whole dance: install the
stubs, drop any previous copy from the module cache, import fresh, return the
module.

The fakes double as test instruments: `events` records calls in order, `clock`
drives ticks_ms, Requests / WLAN / WDT expose flags the suites flip to force
error paths. These were previously embedded in test_boot.py and shared by
slicing its source; that coupling is what F1 removes.
"""
import sys
import time as _real_time

events = []
# ticks_ms source. A list so tests can advance time by assigning clock[0].
clock = [0]
# What machine.reset_cause() reports. A list for the same reason.
machine_reset_cause = [1]   # PWRON_RESET by default


class _Mod:
    """A bare module-like object: attributes from keyword arguments."""

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
    connect_after = None     # succeed after this many connect() calls
    status_code = 0          # what to report while not connected
    pm_set = None

    def __init__(self, iface=None):
        pass

    def active(self, on):
        events.append('wlan_active')

    def status(self, what=None):
        if what == 'rssi':
            return -50
        return 3 if WLAN.connected else WLAN.status_code

    def connect(self, ssid, pw):
        events.append('wlan_connect')
        if WLAN.connect_after is not None:
            WLAN.connect_after -= 1
            if WLAN.connect_after <= 0:
                WLAN.connected = True
                WLAN.connect_after = None

    def disconnect(self):
        events.append('wlan_disconnect')

    def ifconfig(self):
        return ('192.0.2.10', '255.255.255.0', '192.0.2.1', '192.0.2.1')

    def config(self, *args, **kwargs):
        if 'pm' in kwargs:
            WLAN.pm_set = kwargs['pm']
            return None
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
    payload_override = None

    def __init__(self, status=200, payload=None):
        if payload is None and Response.payload_override is not None:
            payload = Response.payload_override
        self.status_code = status
        self._payload = payload if payload is not None else {
            'ok': True, 'result': []}

    def json(self):
        return self._payload

    def close(self):
        events.append('http_close')


class Requests:
    raise_oserror = False
    accepts_timeout = True
    last_timeout = None

    def _check(self, kwargs):
        if 'timeout' in kwargs:
            if not Requests.accepts_timeout:
                raise TypeError("unexpected keyword argument 'timeout'")
            Requests.last_timeout = kwargs['timeout']

    def post(self, url, json=None, **kwargs):
        self._check(kwargs)
        events.append('http_post')
        if Requests.raise_oserror:
            raise OSError(-2, 'DNS lookup failed')
        return Response()

    def get(self, url, **kwargs):
        self._check(kwargs)
        events.append('http_get')
        if Requests.raise_oserror:
            raise OSError(-2, 'DNS lookup failed')
        return Response()


class ResetCalled(Exception):
    """machine.reset() does not return on hardware; neither does the stub."""


def _fake_reset():
    events.append('machine_reset')
    raise ResetCalled()


def _ticks_ms():
    return clock[0]


def _ticks_diff(a, b):
    return a - b


def _ticks_add(a, b):
    return a + b


class _Gc:
    """CPython's gc has no mem_free/mem_alloc; MicroPython's does."""

    def __init__(self):
        import gc as _real
        self._real = _real

    def collect(self):
        return self._real.collect()

    def mem_free(self):
        return 178480

    def mem_alloc(self):
        return 85000


class Ntptime:
    """Controllable ntptime stub.

    next_epoch is returned by time() in the MicroPython 2000 epoch, as the
    real module does. raise_exc makes time() raise, standing in for a lost
    packet or dead server. calls counts invocations so a suite can assert
    that a guard skipped the network entirely.
    """
    next_epoch = 0
    raise_exc = False
    timeout = None
    calls = 0

    @staticmethod
    def time():
        Ntptime.calls += 1
        if Ntptime.raise_exc:
            raise OSError('simulated NTP failure')
        return Ntptime.next_epoch


def install():
    """Install every stub into sys.modules. Run before importing main."""
    sys.modules['gc'] = _Gc()
    sys.modules['rp2'] = _Mod(country=lambda c: events.append('country'))
    sys.modules['network'] = _Mod(WLAN=WLAN, STA_IF='STA_IF',
                                  STAT_IDLE=0, STAT_CONNECTING=1,
                                  STAT_WRONG_PASSWORD=-3,
                                  STAT_NO_AP_FOUND=-2,
                                  STAT_CONNECT_FAIL=-1, STAT_GOT_IP=3)
    sys.modules['machine'] = _Mod(Pin=Pin, mem32=mem32, WDT=WDT,
                                  reset=_fake_reset,
                                  reset_cause=lambda: machine_reset_cause[0],
                                  PWRON_RESET=1, HARD_RESET=2, WDT_RESET=3,
                                  DEEPSLEEP_RESET=4, SOFT_RESET=5)
    sys.modules['ubinascii'] = _Mod(
        hexlify=lambda b, sep=None: b':'.join(b'%02x' % c for c in b))
    sys.modules['urequests'] = Requests()
    sys.modules['ntptime'] = Ntptime()
    sys.modules['board'] = _Mod(doorBellPin=16)
    WLAN.pm_set = None
    sys.modules['secrets'] = _Mod(secrets={
        'ssid': 'net', 'pw': 'pw', 'botToken': 'TOKEN',
        'telegramDmUid': '-1001',
    })
    # gmtime is needed by C1's format_timestamp and is absent from a pure
    # ticks stub. MicroPython's epoch is 2000-01-01; the host's is
    # 1970-01-01, so shift by the difference to keep the two consistent.
    # _real_time is captured at module import, before any install() has
    # replaced sys.modules['time'] with this stub: importing time here
    # instead would, on the second install, wrap the stub's own gmtime and
    # add the epoch offset twice.
    _epoch_2000 = 946684800

    def _gmtime(secs):
        return _real_time.gmtime(secs + _epoch_2000)

    sys.modules['time'] = _Mod(sleep=lambda s: None,
                               sleep_ms=lambda ms: None,
                               ticks_ms=_ticks_ms,
                               ticks_diff=_ticks_diff,
                               ticks_add=_ticks_add,
                               gmtime=_gmtime)


def load_firmware():
    """Install stubs and import main fresh, returning the module.

    Drops any cached copy first so each call re-runs main.py's top level
    against the current stub state, the same isolation the old exec-based
    loader gave by building a new namespace each time.
    """
    install()
    sys.modules.pop('main', None)
    import main
    return main
