####################################################################################
# Tier 1 persistence and reset diagnosis: watchdog scratch registers (E2).
#
# 32 bytes that survive a warm reset but not a power cut, at zero flash cost.
# That asymmetry is the point: if the magic word is still there, power was
# never lost, which is an independent cross-check on machine.reset_cause().
#
# This module owns the scratch layout and the reset diagnosis: reading the
# three sources, deciding the verdict, and formatting the "Reset:" line. It
# depends only on machine, so it is a leaf. Callers never touch a scratch
# index; they use mark_intended_reset() and clear_unstable_count().
#
# Boot accounting is deliberately not here. When a boot counts as stable,
# and when that is worth a flash write, is policy that ties persist and this
# module together, so it stays with the caller (main's boot() and
# mark_boot_stable()). So does self_reset(), which must also snapshot the
# ring queue before it resets.
####################################################################################

import machine

WATCHDOG_BASE = 0x40058000
WATCHDOG_REASON = WATCHDOG_BASE + 0x08
WATCHDOG_SCRATCH0 = WATCHDOG_BASE + 0x0c   # SCRATCH0..7, four bytes each

# Scratch 4-7 carry the bootrom's reboot-to-BOOTSEL handshake, so stay in
# 0-3. Using the top of that range leaves 0 and 1 alone in case the port
# wants them.
# Set immediately before a reset this firmware chose to perform, so the next
# boot can tell its own doing from a watchdog bite. Both look identical
# otherwise: warm, with no CHIP_RESET flags.
# UNVERIFIED: scratch 0 and 1 are believed unused by the port, but only
# 4-7 are documented as taken by the bootrom.
SCRATCH_REASON_IDX = 0        # why self_reset() fired, for the next boot
REASON_NAMES = {
    0: '',
    1: 'WiFi unreachable',
    2: 'network dead after a bounce',
}
REASON_WIFI = 1
REASON_NETWORK = 2

SCRATCH_INTENT_IDX = 1
SCRATCH_INTENT = 0x50444252   # 'PDBR'

SCRATCH_MAGIC_IDX = 2
# Counts boots since the last one that proved stable. Not a total: any
# hardware reset clears the scratch area, so a running total has to live in
# flash (see 'boots' in state.json). This one exists to make boot loops
# visible, which is precisely the case where flash must not be written.
SCRATCH_UNSTABLE_IDX = 3

SCRATCH_MAGIC = 0x50444231   # 'PDB1'

# RP2040 VREG_AND_CHIP_RESET.CHIP_RESET. Records what caused the last reset
# in hardware, independently of what MicroPython reports.
# UNVERIFIED: bit positions are from the datasheet but have not been
# confirmed on a board. The raw word is logged as well, so a wrong decode
# here cannot destroy the underlying evidence.
CHIP_RESET = 0x40064000 + 0x08
CHIP_RESET_HAD_POR = 1 << 8          # power-on or brown-out
CHIP_RESET_HAD_RUN = 1 << 16         # RUN pin pulled low
CHIP_RESET_HAD_PSM_RESTART = 1 << 20 # restart from the debug port

def scratch_read(index):
    return machine.mem32[WATCHDOG_SCRATCH0 + (index * 4)]

def scratch_write(index, value):
    machine.mem32[WATCHDOG_SCRATCH0 + (index * 4)] = value & 0xFFFFFFFF

def reset_cause_name(value):
    # Which constants exist varies by port and version, so match by lookup
    # rather than assuming any particular one is defined.
    for name in ('PWRON_RESET', 'HARD_RESET', 'WDT_RESET',
                 'DEEPSLEEP_RESET', 'SOFT_RESET'):
        if getattr(machine, name, None) == value:
            return name
    return 'UNKNOWN_' + str(value)

def decode_chip_reset(word):
    flags = []
    if word & CHIP_RESET_HAD_POR:
        flags.append('POR/BOD')
    if word & CHIP_RESET_HAD_RUN:
        flags.append('RUN')
    if word & CHIP_RESET_HAD_PSM_RESTART:
        flags.append('DEBUG')
    return flags

def read_reset_info():
    """Capture why we restarted, and how many times.

    Three independent sources, because no single one is trustworthy on its
    own for the question we are asking:

      - machine.reset_cause(), the port's own interpretation
      - CHIP_RESET, the hardware's record: separates a supply brownout
        (POR/BOD) from the RUN pin being pulled low
      - the scratch magic word, which survives a warm reset but not a
        power cut, so its absence independently confirms power was lost

    Never raises. Diagnostics must not be able to stop the device booting.
    """
    info = {
        'cause': 'unavailable',
        'causeRaw': None,
        'chipReset': None,
        'flags': [],
        'wdtReason': None,
        'unstableBoots': 0,
        'bootNumber': None,
        'selfReset': False,
        'resetReason': '',
        'warmBoot': False,
    }
    try:
        raw = machine.reset_cause()
        info['causeRaw'] = raw
        info['cause'] = reset_cause_name(raw)
    except Exception:
        pass
    try:
        word = machine.mem32[CHIP_RESET]
        info['chipReset'] = word
        info['flags'] = decode_chip_reset(word)
    except Exception:
        pass
    try:
        info['wdtReason'] = machine.mem32[WATCHDOG_REASON]
    except Exception:
        pass
    try:
        if scratch_read(SCRATCH_INTENT_IDX) == SCRATCH_INTENT:
            info['selfReset'] = True
            info['resetReason'] = REASON_NAMES.get(
                scratch_read(SCRATCH_REASON_IDX), '')
            scratch_write(SCRATCH_INTENT_IDX, 0)
            scratch_write(SCRATCH_REASON_IDX, 0)
    except Exception:
        pass
    try:
        if scratch_read(SCRATCH_MAGIC_IDX) == SCRATCH_MAGIC:
            # Magic intact: nothing reset the chip, so this was a soft
            # reboot or a watchdog bite.
            info['warmBoot'] = True
            info['unstableBoots'] = scratch_read(SCRATCH_UNSTABLE_IDX) + 1
        else:
            info['unstableBoots'] = 1
            scratch_write(SCRATCH_MAGIC_IDX, SCRATCH_MAGIC)
        scratch_write(SCRATCH_UNSTABLE_IDX, info['unstableBoots'])
    except Exception:
        pass
    return info

def reset_verdict(info):
    """Decide what actually caused the reset.

    Deliberately ignores machine.reset_cause() and WATCHDOG_REASON. Both
    are unreliable on rp2: the bootrom uses the watchdog to launch the
    application, so REASON carries the TIMER bit through ordinary
    startup. Observed on hardware, same board, minutes apart:
    cause=WDT_RESET on a soft reboot, then cause=PWRON_RESET on a RUN-pin
    reset. Neither was right.

    CHIP_RESET and the scratch magic are trustworthy, and were both
    confirmed on hardware: POR reads 0x00000100, RUN reads 0x00010000,
    and the bits do not accumulate -- each reset reports only its own
    cause.

    The scratch area is cleared by any hardware reset, RUN included, not
    only by power loss. So a surviving magic word means no hardware reset
    occurred, which in turn means CHIP_RESET still describes some earlier
    event and must be ignored.
    """
    if info is None:
        return 'unknown'
    if info['selfReset']:
        # We asked for this one. Without the marker it would be
        # indistinguishable from a watchdog bite.
        return 'self-reset'
    if info['warmBoot']:
        # Scratch survived, so nothing reset the chip: a soft reboot or a
        # watchdog bite. CHIP_RESET here is stale.
        #
        # WATCHDOG_REASON separates them. Bit 0 is TIMER, an actual timeout;
        # bit 1 is FORCE, which machine.reset() uses. Observed on the bench:
        # wdt=0x1 for a genuine bite during a slow request, wdt=0x2 for a
        # deliberate reset. Only trusted on a warm boot -- the bootrom sets
        # TIMER during an ordinary cold start.
        reason = info['wdtReason'] or 0
        if reason & 0x1:
            return 'watchdog'
        return 'warm-reset'
    flags = info['flags']
    if 'POR/BOD' in flags:
        # Supply dropped, or this is the first power-up.
        return 'power'
    if 'RUN' in flags:
        return 'run-pin'
    return 'unknown'

def format_reset_info(info):
    if info is None:
        return 'Reset info unavailable'
    number = info['bootNumber']
    parts = ['boot #' + (str(number) if number is not None else '?'),
             'verdict=' + reset_verdict(info)]
    if info['unstableBoots'] > 1:
        # More than one attempt since the last stable boot: a loop.
        parts.append('unstable=' + str(info['unstableBoots']))
    if info['flags']:
        # Stale on a warm boot -- see reset_verdict().
        label = 'chip' if not info['warmBoot'] else 'chip(stale)'
        parts.append(label + '=' + '+'.join(info['flags']))
    if info['chipReset'] is not None:
        parts.append('raw=0x%08x' % info['chipReset'])
    if info['resetReason']:
        parts.append('reason=' + info['resetReason'].replace(' ', '_'))
    parts.append('warm' if info['warmBoot'] else 'cold')
    # Advisory only. Kept because it is free and occasionally corroborates.
    advisory = 'cause=' + str(info['cause'])
    if info['wdtReason']:
        advisory += ' wdt=0x%x' % info['wdtReason']
    parts.append('(' + advisory + ')')
    return 'Reset: ' + ' '.join(parts)

def mark_intended_reset(code=0):
    """Mark the next reset as self-inflicted, with a short reason code.

    Call immediately before machine.reset(). The reason goes in first and
    the intent marker last: read_reset_info() reads the reason only when
    the marker is present, so an interruption between the two writes leaves
    a reset that reads as unintended, never one with a wrong reason.
    Never raises; a failed marker must not stop the reset.
    """
    try:
        scratch_write(SCRATCH_REASON_IDX, code)
    except Exception:
        pass
    try:
        scratch_write(SCRATCH_INTENT_IDX, SCRATCH_INTENT)
    except Exception:
        pass

def clear_unstable_count():
    """Attempts since the last stable boot are now history. Never raises."""
    try:
        scratch_write(SCRATCH_UNSTABLE_IDX, 0)
    except Exception:
        pass
