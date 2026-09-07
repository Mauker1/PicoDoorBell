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
UNSTABLE_ADDR = SCRATCH0 + 3 * 4
CHIP_RESET = 0x40064000 + 0x08
WDT_REASON = 0x40058000 + 0x08

# --- 1. Cold boot: scratch registers hold garbage --------------------------
mem32.cells.clear()
mem32[MAGIC_ADDR] = 0xDEADBEEF          # not our magic
mem32[CHIP_RESET] = 1 << 8              # HAD_POR
set_reset_cause(1)                      # PWRON_RESET
ns = load_firmware()
info = ns['resetInfo']

check('cold boot is attempt 1', info['unstableBoots'], 1)
check('cold boot flagged as cold', info['warmBoot'], False)
check('cause name resolved', info['cause'], 'PWRON_RESET')
check('POR decoded from chip register', info['flags'], ['POR/BOD'])
check('magic word written for next boot', mem32[MAGIC_ADDR], 0x50444231)
check('attempt counter written to scratch', mem32[UNSTABLE_ADDR], 1)

# --- 2. Warm boot: magic survived, so power was never lost -----------------
mem32[CHIP_RESET] = 1 << 16             # HAD_RUN
set_reset_cause(2)                      # HARD_RESET
ns = load_firmware()
info = ns['resetInfo']

check('warm boot increments the attempt counter', info['unstableBoots'], 2)
check('warm boot flagged as warm', info['warmBoot'], True)
check('RUN pin decoded', info['flags'], ['RUN'])
check('cause reported as hard reset', info['cause'], 'HARD_RESET')

ns = load_firmware()
check('attempts keep climbing', ns['resetInfo']['unstableBoots'], 3)

# --- 3. Power cut clears the scratch registers -----------------------------
# A real power-on reset zeroes them, which is exactly how we detect it
# independently of what reset_cause() claims.
mem32.cells.clear()
mem32[CHIP_RESET] = 1 << 8
set_reset_cause(1)
ns = load_firmware()
info = ns['resetInfo']

check('power cut restarts the attempt count', info['unstableBoots'], 1)
check('power cut detected as cold', info['warmBoot'], False)

# --- 4. Watchdog reset, once B2 exists -------------------------------------
mem32[MAGIC_ADDR] = 0x50444231
mem32[UNSTABLE_ADDR] = 41
mem32[WDT_REASON] = 1                   # TIMER
mem32[CHIP_RESET] = 0
set_reset_cause(3)                      # WDT_RESET
ns = load_firmware()
info = ns['resetInfo']

check('watchdog cause identified', info['cause'], 'WDT_RESET')
check('watchdog reason captured', info['wdtReason'], 1)
check('watchdog reset counted as warm', info['warmBoot'], True)
check('attempts continued from scratch', info['unstableBoots'], 42)

# --- 4b. Verdicts, against real hardware observations ----------------------
# Values below are exactly what the bench unit reported.

mem32.cells.clear()
mem32[CHIP_RESET] = 0x00000100          # observed on a power-on boot
set_reset_cause(3)                      # reset_cause said WDT_RESET -- wrong
ns = load_firmware()
check('power-on verdict ignores a wrong cause',
      ns['reset_verdict'](ns['resetInfo']), 'power')

mem32.cells.clear()
mem32[CHIP_RESET] = 0x00010000          # observed after pressing reset
set_reset_cause(1)                      # reset_cause said PWRON_RESET -- wrong
ns = load_firmware()
check('RUN verdict ignores a wrong cause',
      ns['reset_verdict'](ns['resetInfo']), 'run-pin')
check('RUN reset reads as cold', ns['resetInfo']['warmBoot'], False)

# Scratch intact means no hardware reset, so CHIP_RESET is stale.
mem32[MAGIC_ADDR] = 0x50444231
mem32[UNSTABLE_ADDR] = 7
mem32[CHIP_RESET] = 0x00000100          # left over from an earlier power-up
ns = load_firmware()
check('warm boot reports a warm reset',
      ns['reset_verdict'](ns['resetInfo']), 'warm-reset')
check('warm boot does not misread stale POR as power',
      ns['reset_verdict'](ns['resetInfo']) != 'power', True)
line = ns['format_reset_info'](ns['resetInfo'])
check('stale chip flags are labelled', 'chip(stale)=' in line, True)
check('advisory cause is bracketed', '(cause=' in line, True)

mem32.cells.clear()
mem32[CHIP_RESET] = 0                   # no flags, no scratch
ns = load_firmware()
check('no evidence reads as unknown',
      ns['reset_verdict'](ns['resetInfo']), 'unknown')

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
# Both bits at once is not something hardware produces -- confirmed on the
# bench that flags do not accumulate -- but the formatter should cope.
check('summary names both flags', 'POR/BOD+RUN' in line, True)
check('summary carries the raw word', 'raw=0x00010100' in line, True)
check('summary reports boot number', 'boot #1' in line, True)

# --- 9. C5: the boot number is durable, the attempt counter is not ---------
import json as _json

def stable_boot(ns):
    """Run the loop's stability gate as if BOOT_STABLE_MS had elapsed."""
    ns['bootStableAt'] = -1
    ns['mark_boot_stable']()

mem32.cells.clear()
mem32[CHIP_RESET] = 1 << 8
set_reset_cause(1)
try:
    os.remove('state.json')
except OSError:
    pass

ns = load_firmware()
check('first boot is number 1', ns['bootNumber'], 1)
check('no flash write before proving stable', os.path.exists('state.json'), False)
stable_boot(ns)
check('stable boot is persisted', _json.load(open('state.json'))['boots'], 1)
check('attempt counter cleared once stable', mem32[UNSTABLE_ADDR], 0)

# A power cycle clears the scratch area but must not lose the total.
mem32.cells.clear()
mem32[CHIP_RESET] = 1 << 8
ns = load_firmware()
check('boot number survives a power cycle', ns['bootNumber'], 2)
check('verdict still reads power', ns['reset_verdict'](ns['resetInfo']), 'power')
stable_boot(ns)
check('total advanced to 2', _json.load(open('state.json'))['boots'], 2)

# A RUN reset likewise -- this is what Tier 1 could not do.
mem32.cells.clear()
mem32[CHIP_RESET] = 1 << 16
ns = load_firmware()
check('boot number survives a RUN reset', ns['bootNumber'], 3)

# A boot loop must never reach flash.
before = _json.load(open('state.json'))['writes']
for _ in range(20):
    mem32.cells.clear()
    mem32[CHIP_RESET] = 1 << 8
    load_firmware()          # crashes out before the gate, as a loop would
after = _json.load(open('state.json'))['writes']
check('twenty unstable boots wrote nothing', after, before)

# Repeated warm resets accumulate visibly.
mem32.cells.clear()
mem32[MAGIC_ADDR] = 0x50444231
mem32[UNSTABLE_ADDR] = 4
ns = load_firmware()
check('unstable attempts counted', ns['resetInfo']['unstableBoots'], 5)
check('summary flags the loop',
      'unstable=5' in ns['format_reset_info'](ns['resetInfo']), True)

# Schema bump: a v1 file migrates and starts counting from zero.
open('state.json', 'w').write('{"v": 1, "chatId": -5, "epochAnchor": null, "writes": 9}')
mem32.cells.clear()
mem32[CHIP_RESET] = 1 << 8
ns = load_firmware()
check('an older file migrates to the current version',
      ns['state']['v'], ns['STATE_VERSION'])
check('migration preserves chatId', ns['state']['chatId'], -5)
check('absent boots field defaults to zero', ns['state']['boots'], 0)

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
    check('unreadable registers give attempt count 0',
          ns['resetInfo']['unstableBoots'], 0)
finally:
    sys.modules['machine'].mem32 = saved

print()
print('%d/%d passed' % (sum(results), len(results)))
raise SystemExit(0 if all(results) else 1)
