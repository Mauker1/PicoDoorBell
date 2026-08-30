"""Verify C4: reset cause capture and the Tier 1 boot counter.

The question this feature exists to answer: the production unit reboots
intermittently, correlated with switching mains loads on the same
circuit, and every restart currently looks identical. Three independent
signals are captured so the cause can be narrowed:

  - machine.reset_cause()      the port's interpretation
  - CHIP_RESET                 hardware's record; separates a supply
                               brownout (POR/BOD) from the RUN pin
  - scratch magic word         survives a warm reset but not a power
                               cut, so its absence confirms power loss

Reuses the stub hardware from test_boot.py.
"""
import ast
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', 'main.py')

# Reuse the stubs without running test_boot's own assertions.
_src = open(os.path.join(HERE, 'test_boot.py')).read()
_stubs = {'__name__': 'stubs', '__file__': os.path.join(HERE, 'test_boot.py')}
exec(compile(_src[:_src.index('work = tempfile.mkdtemp()')], 'stubs', 'exec'), _stubs)
_stubs['install_stubs']()

mem32 = _stubs['mem32']
Requests = _stubs['Requests']

results = []


def check(label, got, expected):
    ok = got == expected
    results.append(ok)
    print('%-4s %-52s got=%r' % ('ok' if ok else 'FAIL', label, got))


def load_firmware():
    tree = ast.parse(open(SRC).read())
    tree.body = [n for n in tree.body if not isinstance(n, ast.While)]
    ns = {'__name__': 'main'}
    exec(compile(tree, 'main.py', 'exec'), ns)
    return ns


def set_reset_cause(value):
    _stubs['machine_reset_cause'][0] = value


os.chdir(tempfile.mkdtemp())
Requests.raise_oserror = False

# Addresses under test, mirrored from main.py.
SCRATCH0 = 0x40058000 + 0x0c
MAGIC_ADDR = SCRATCH0 + 2 * 4
COUNT_ADDR = SCRATCH0 + 3 * 4
CHIP_RESET = 0x40064000 + 0x08
WDT_REASON = 0x40058000 + 0x08

# --- 1. Cold boot: scratch registers hold garbage --------------------------
mem32.cells.clear()
mem32[MAGIC_ADDR] = 0xDEADBEEF          # not our magic
mem32[CHIP_RESET] = 1 << 8              # HAD_POR
set_reset_cause(1)                      # PWRON_RESET
ns = load_firmware()
info = ns['resetInfo']

check('cold boot counts as boot 1', info['bootCount'], 1)
check('cold boot flagged as cold', info['warmBoot'], False)
check('cause name resolved', info['cause'], 'PWRON_RESET')
check('POR decoded from chip register', info['flags'], ['POR/BOD'])
check('magic word written for next boot', mem32[MAGIC_ADDR], 0x50444231)
check('counter persisted to scratch', mem32[COUNT_ADDR], 1)

# --- 2. Warm boot: magic survived, so power was never lost -----------------
mem32[CHIP_RESET] = 1 << 16             # HAD_RUN
set_reset_cause(2)                      # HARD_RESET
ns = load_firmware()
info = ns['resetInfo']

check('warm boot increments the counter', info['bootCount'], 2)
check('warm boot flagged as warm', info['warmBoot'], True)
check('RUN pin decoded', info['flags'], ['RUN'])
check('cause reported as hard reset', info['cause'], 'HARD_RESET')

ns = load_firmware()
check('counter keeps climbing', ns['resetInfo']['bootCount'], 3)

# --- 3. Power cut clears the scratch registers -----------------------------
# A real power-on reset zeroes them, which is exactly how we detect it
# independently of what reset_cause() claims.
mem32.cells.clear()
mem32[CHIP_RESET] = 1 << 8
set_reset_cause(1)
ns = load_firmware()
info = ns['resetInfo']

check('power cut restarts the count', info['bootCount'], 1)
check('power cut detected as cold', info['warmBoot'], False)

# --- 4. Watchdog reset, once B2 exists -------------------------------------
mem32[MAGIC_ADDR] = 0x50444231
mem32[COUNT_ADDR] = 41
mem32[WDT_REASON] = 1                   # TIMER
mem32[CHIP_RESET] = 0
set_reset_cause(3)                      # WDT_RESET
ns = load_firmware()
info = ns['resetInfo']

check('watchdog cause identified', info['cause'], 'WDT_RESET')
check('watchdog reason captured', info['wdtReason'], 1)
check('watchdog reset counted as warm', info['warmBoot'], True)
check('counter continued from scratch', info['bootCount'], 42)

# --- 5. The discriminator the production unit needs ------------------------
# Brownout and RUN-pin pickup must not look alike.
mem32.cells.clear()
mem32[CHIP_RESET] = 1 << 8
set_reset_cause(1)
brownout = load_firmware()['resetInfo']

mem32.cells.clear()
mem32[CHIP_RESET] = 1 << 16
set_reset_cause(2)
runpin = load_firmware()['resetInfo']

check('brownout and RUN pickup are distinguishable',
      brownout['flags'] != runpin['flags'], True)
check('brownout reads as supply', brownout['flags'], ['POR/BOD'])
check('RUN pickup reads as pin', runpin['flags'], ['RUN'])

# --- 6. Unknown cause codes degrade readably -------------------------------
set_reset_cause(99)
ns = load_firmware()
check('unknown cause is reported, not hidden',
      ns['resetInfo']['cause'], 'UNKNOWN_99')

# --- 7. Summary line is human-readable -------------------------------------
mem32.cells.clear()
mem32[CHIP_RESET] = (1 << 8) | (1 << 16)
set_reset_cause(1)
ns = load_firmware()
line = ns['format_reset_info'](ns['resetInfo'])
check('summary names both flags', 'POR/BOD+RUN' in line, True)
check('summary carries the raw word', 'raw=0x00010100' in line, True)
check('summary reports boot number', 'boot #1' in line, True)

# --- 8. Diagnostics must never stop the boot -------------------------------
class ExplodingMem:
    def __getitem__(self, addr):
        raise RuntimeError('no such register')

    def __setitem__(self, addr, value):
        raise RuntimeError('no such register')


saved = sys.modules['machine'].mem32
sys.modules['machine'].mem32 = ExplodingMem()
try:
    ns = load_firmware()
    check('boot survives unreadable registers', ns['doorBellInput'] is not None, True)
    check('reset info degrades rather than raising',
          ns['resetInfo']['cause'] is not None, True)
    check('unreadable registers give boot count 0',
          ns['resetInfo']['bootCount'], 0)
finally:
    sys.modules['machine'].mem32 = saved

print()
print('%d/%d passed' % (sum(results), len(results)))
raise SystemExit(0 if all(results) else 1)