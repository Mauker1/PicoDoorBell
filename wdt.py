####################################################################################
# B2 watchdog: the "do not starve the dog" primitive (E2).
#
# The RP2040 watchdog cannot exceed roughly 8.3 s, uncomfortably close to what
# one loop pass can legitimately take: a Telegram round trip is 1-2 s measured,
# and a worst-case pass can hold a send, a getUpdates and a flash sector erase.
# The margin is bought by feeding from inside the blocking work, not only at the
# top of the loop, which is why sleep_fed and feed_watchdog live here and are
# called from deep inside net, led, and the flash path.
#
# A leaf module: it imports only the stdlib and machine, so anything may depend
# on it without a cycle, the way anything may depend on config. In particular
# arm_watchdog does not log; it returns a message and lets the caller (boot)
# report it, so this module never reaches up into applog.
#
# Once armed, an RP2040 watchdog cannot be disarmed.
####################################################################################

import machine
import time

import config

# The live watchdog, or None before arming / if arming failed. Feeding a None
# watchdog is a no-op, so callers never have to check.
wdt = None


def arm_watchdog():
    """Start the watchdog and return a status message for the caller to log.

    Armed after hardware and state are up but before networking, because a
    wedged cyw43 stack is the failure this is chiefly for. Not armed any
    earlier: error_halt() blinks forever by design, and a watchdog would
    turn a configuration mistake into a silent reset loop instead of a
    visible fault.

    Returns the message rather than logging it, so this module stays a leaf
    with no dependency on applog. boot() reports what comes back.
    """
    global wdt
    if wdt is not None:
        return None
    try:
        wdt = machine.WDT(timeout=config.WDT_TIMEOUT_MS)
        return 'Watchdog armed at ' + str(config.WDT_TIMEOUT_MS) + 'ms'
    except Exception as e:
        # An unguarded device still answers the door. One that refuses to
        # start does not.
        return 'Watchdog unavailable: ' + str(e)


def feed_watchdog():
    if wdt is not None:
        try:
            wdt.feed()
        except Exception:
            pass


def sleep_fed(seconds):
    """Sleep without letting the watchdog bite.

    Any wait longer than the timeout has to be broken up. The 10 s grace
    period after an error was the clearest example: left whole, it would
    have reset the device every time anything went wrong.
    """
    remaining = int(seconds * 1000)
    while remaining > 0:
        feed_watchdog()
        step = 500 if remaining > 500 else remaining
        time.sleep_ms(step)
        remaining -= step
    feed_watchdog()
