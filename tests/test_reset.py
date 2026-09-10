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

Imports main under the host-side stubs (F1). Each scenario sets the mem32
registers, then boots: resetInfo, bootNumber and the rest are populated by
boot(), which no longer runs at import (the __name__ guard), so this suite
calls it explicitly through load_and_boot().
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', 'main.py')
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, HERE)
import stubs

mem32 = stubs.mem32
Requests = stubs.Requests

results = []


def check(label, got, expected):
    ok = got == expected
    results.append(ok)
    print('%-4s %-52s got=%r' % ('ok' if ok else 'FAIL', label, got))


def load_and_boot():
    """Import main fresh and run boot(), the way a real start would.

    boot() reads the reset registers, configures hardware, loads state and
    sets bootNumber, all of which this suite inspects. WiFi is up (the stub
    default) so connect_wifi() returns at once and the startup send is a
    no-op through the Requests stub.
    """
    stubs.WLAN.connected = True
    m = stubs.load_firmware()
    m.boot()
    return m


def set_reset_cause(value):
    stubs.machine_reset_cause[0] = value


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
m = load_and_boot()
info = m.resetInfo

check('cold boot is attempt 1', info['unstableBoots'], 1)
check('cold boot flagged as cold', info['warmBoot'], False)
check('cause name resolved', info['cause'], 'PWRON_RESET')
check('POR decoded from chip register', info['flags'], ['POR/BOD'])
check('magic word written for next boot', mem32[MAGIC_ADDR], 0x50444231)
check('attempt counter written to scratch', mem32[UNSTABLE_ADDR], 1)

# --- 2. Warm boot: magic survived, so power was never lost -----------------
mem32[CHIP_RESET] = 1 << 16             # HAD_RUN
set_reset_cause(2)                      # HARD_RESET
m = load_and_boot()
info = m.resetInfo

check('warm boot increments the attempt counter', info['unstableBoots'], 2)
check('warm boot flagged as warm', info['warmBoot'], True)
check('RUN pin decoded', info['flags'], ['RUN'])
check('cause reported as hard reset', info['cause'], 'HARD_RESET')

m = load_and_boot()
check('attempts keep climbing', m.resetInfo['unstableBoots'], 3)

# --- 3. Power cut clears the scratch registers -----------------------------
# A real power-on reset zeroes them, which is exactly how we detect it
# independently of what reset_cause() claims.
mem32.cells.clear()
mem32[CHIP_RESET] = 1 << 8
set_reset_cause(1)
m = load_and_boot()
info = m.resetInfo

check('power cut restarts the attempt count', info['unstableBoots'], 1)
check('power cut detected as cold', info['warmBoot'], False)

# --- 4. Watchdog reset, once B2 exists -------------------------------------
mem32[MAGIC_ADDR] = 0x50444231
mem32[UNSTABLE_ADDR] = 41
mem32[WDT_REASON] = 1                   # TIMER
mem32[CHIP_RESET] = 0
set_reset_cause(3)                      # WDT_RESET
m = load_and_boot()
info = m.resetInfo

check('watchdog cause identified', info['cause'], 'WDT_RESET')
check('watchdog reason captured', info['wdtReason'], 1)
check('watchdog reset counted as warm', info['warmBoot'], True)
check('attempts continued from scratch', info['unstableBoots'], 42)

# --- 4b. Verdicts, against real hardware observations ----------------------
# Values below are exactly what the bench unit reported.

mem32.cells.clear()
mem32[CHIP_RESET] = 0x00000100          # observed on a power-on boot
set_reset_cause(3)                      # reset_cause said WDT_RESET, wrong
m = load_and_boot()
check('power-on verdict ignores a wrong cause',
      m.reset_verdict(m.resetInfo), 'power')

mem32.cells.clear()
mem32[CHIP_RESET] = 0x00010000          # observed after pressing reset
set_reset_cause(1)                      # reset_cause said PWRON_RESET, wrong
m = load_and_boot()
check('RUN verdict ignores a wrong cause',
      m.reset_verdict(m.resetInfo), 'run-pin')
check('RUN reset reads as cold', m.resetInfo['warmBoot'], False)

# Scratch intact means no hardware reset, so CHIP_RESET is stale.
mem32[MAGIC_ADDR] = 0x50444231
mem32[UNSTABLE_ADDR] = 7
mem32[CHIP_RESET] = 0x00000100          # left over from an earlier power-up
m = load_and_boot()
check('warm boot reports a warm reset',
      m.reset_verdict(m.resetInfo), 'warm-reset')
check('warm boot does not misread stale POR as power',
      m.reset_verdict(m.resetInfo) != 'power', True)
line = m.format_reset_info(m.resetInfo)
check('stale chip flags are labelled', 'chip(stale)=' in line, True)
check('advisory cause is bracketed', '(cause=' in line, True)

mem32.cells.clear()
mem32[CHIP_RESET] = 0                   # no flags, no scratch
m = load_and_boot()
check('no evidence reads as unknown',
      m.reset_verdict(m.resetInfo), 'unknown')

# A genuine watchdog bite is distinguishable from a soft reboot. Observed on
# the bench: wdt=0x1 (TIMER) for a real timeout during a slow request,
# wdt=0x2 (FORCE) for machine.reset().
mem32.cells.clear()
mem32[MAGIC_ADDR] = 0x50444231
mem32[WDT_REASON] = 0x1
mem32[CHIP_RESET] = 0
m = load_and_boot()
check('a TIMER bite reads as a watchdog reset',
      m.reset_verdict(m.resetInfo), 'watchdog')

mem32[WDT_REASON] = 0x2
m = load_and_boot()
check('a FORCE reset does not', m.reset_verdict(m.resetInfo), 'warm-reset')

# --- 5. The discriminator the production unit needs ------------------------
# Brownout and RUN-pin pickup must not look alike.
mem32.cells.clear()
mem32[CHIP_RESET] = 1 << 8
set_reset_cause(1)
brownout = load_and_boot().resetInfo

mem32.cells.clear()
mem32[CHIP_RESET] = 1 << 16
set_reset_cause(2)
runpin = load_and_boot().resetInfo

check('brownout and RUN pickup are distinguishable',
      brownout['flags'] != runpin['flags'], True)
check('brownout reads as supply', brownout['flags'], ['POR/BOD'])
check('RUN pickup reads as pin', runpin['flags'], ['RUN'])

# --- 6. Unknown cause codes degrade readably -------------------------------
set_reset_cause(99)
m = load_and_boot()
check('unknown cause is reported, not hidden',
      m.resetInfo['cause'], 'UNKNOWN_99')

# --- 7. Summary line is human-readable -------------------------------------
mem32.cells.clear()
mem32[CHIP_RESET] = (1 << 8) | (1 << 16)
set_reset_cause(1)
m = load_and_boot()
line = m.format_reset_info(m.resetInfo)
# Both bits at once is not something hardware produces (confirmed on the
# bench that flags do not accumulate) but the formatter should cope.
check('summary names both flags', 'POR/BOD+RUN' in line, True)
check('summary carries the raw word', 'raw=0x00010100' in line, True)
check('summary reports boot number', 'boot #1' in line, True)

# --- 9. C5: the boot number is durable, the attempt counter is not ---------
import json as _json


def stable_boot(m):
    """Run the loop's stability gate as if BOOT_STABLE_MS had elapsed."""
    m.bootStableAt = -1
    m.mark_boot_stable()


mem32.cells.clear()
mem32[CHIP_RESET] = 1 << 8
set_reset_cause(1)
try:
    os.remove('state.json')
except OSError:
    pass

m = load_and_boot()
check('first boot is number 1', m.bootNumber, 1)
check('no flash write before proving stable', os.path.exists('state.json'), False)
stable_boot(m)
check('stable boot is persisted', _json.load(open('state.json'))['boots'], 1)
check('attempt counter cleared once stable', mem32[UNSTABLE_ADDR], 0)

# A power cycle clears the scratch area but must not lose the total.
mem32.cells.clear()
mem32[CHIP_RESET] = 1 << 8
m = load_and_boot()
check('boot number survives a power cycle', m.bootNumber, 2)
check('verdict still reads power', m.reset_verdict(m.resetInfo), 'power')
stable_boot(m)
check('total advanced to 2', _json.load(open('state.json'))['boots'], 2)

# A RUN reset likewise: this is what Tier 1 could not do.
mem32.cells.clear()
mem32[CHIP_RESET] = 1 << 16
m = load_and_boot()
check('boot number survives a RUN reset', m.bootNumber, 3)

# A boot loop must never reach flash.
before = _json.load(open('state.json'))['writes']
for _ in range(20):
    mem32.cells.clear()
    mem32[CHIP_RESET] = 1 << 8
    load_and_boot()          # never reaches the stability gate, as a loop would
after = _json.load(open('state.json'))['writes']
check('twenty unstable boots wrote nothing', after, before)

# Repeated warm resets accumulate visibly.
mem32.cells.clear()
mem32[MAGIC_ADDR] = 0x50444231
mem32[UNSTABLE_ADDR] = 4
m = load_and_boot()
check('unstable attempts counted', m.resetInfo['unstableBoots'], 5)
check('summary flags the loop',
      'unstable=5' in m.format_reset_info(m.resetInfo), True)

# Schema bump: a v1 file migrates and starts counting from zero.
open('state.json', 'w').write('{"v": 1, "chatId": -5, "epochAnchor": null, "writes": 9}')
mem32.cells.clear()
mem32[CHIP_RESET] = 1 << 8
m = load_and_boot()
check('an older file migrates to the current version',
      m.state['v'], m.STATE_VERSION)
check('migration preserves chatId', m.state['chatId'], -5)
check('absent boots field defaults to zero', m.state['boots'], 0)

# --- 8. Diagnostics must never stop the boot -------------------------------
class ExplodingMem:
    def __getitem__(self, addr):
        raise RuntimeError('no such register')

    def __setitem__(self, addr, value):
        raise RuntimeError('no such register')


# Patch the stub module's mem32, since install() rebinds machine.mem32 from
# it on every load; swapping only sys.modules['machine'].mem32 would be undone
# by the next load_and_boot().
saved = stubs.mem32
stubs.mem32 = ExplodingMem()
try:
    m = load_and_boot()
    check('boot survives unreadable registers', m.doorBellInput is not None, True)
    check('reset info degrades rather than raising',
          m.resetInfo['cause'] is not None, True)
    check('unreadable registers give attempt count 0',
          m.resetInfo['unstableBoots'], 0)
finally:
    stubs.mem32 = saved

print()
print('%d/%d passed' % (sum(results), len(results)))
raise SystemExit(0 if all(results) else 1)
