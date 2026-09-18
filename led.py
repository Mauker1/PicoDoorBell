####################################################################################
# G1 LED state machine (E2).
#
# The onboard LED is the only local feedback the device has, so it must never
# lie. Before G1, LED calls were scattered: connect_wifi set it solid on, each
# failed attempt turned it off, and the main loop's error handler turned it back
# *on* right after wlan.disconnect(), leaving the "connected" light lit on a
# disconnected board.
#
# The fix is a single source of truth. A steady state (off, connecting,
# connected, error) is recorded here and rendered by set_led_state; callers
# never set a bare level. Discrete events (the connect confirmation, a
# delivered ring, the fatal blink) are short bursts that restore the steady
# state when they finish.
#
# This module renders states and runs blink bursts; it does not decide *when*
# to change state. That policy (connecting while joining, alert on delivery)
# stays with the callers in net and telegram. Its one dependency is wdt, for
# watchdog-safe blink timing: a blink burst must not starve the dog. The pin
# is injected via init(); the CYW43-hosted LED does not exist until the radio
# is up, so setup_hardware creates it and hands it here.
#
# The RP2040 has no spare timer wired to this, so there is no interrupt-driven
# pattern: steady states are solid levels, and the connecting blink is driven
# by the connect loop, which already polls and toggles once per pass.
####################################################################################

import wdt

LED_OFF = 0          # idle, or not yet connected
LED_CONNECTING = 1   # a join is under way (poll-driven blink)
LED_CONNECTED = 2    # associated, solid on
LED_ERROR = 3        # unrecoverable setup fault (fatal, fast blink)

# Solid level each steady state shows at rest. Connecting has no rest level:
# the connect loop toggles it. Error never rests: error_halt blinks forever.
LED_STEADY_LEVEL = {
    LED_OFF: 0,
    LED_CONNECTED: 1,
}

# The pin, injected by init(). None until then; every function tolerates None
# so the LED is never a reason to fail an operation.
led = None
ledState = LED_OFF


def init(pin):
    """Hand the module the LED pin and set a known resting state.

    setup_hardware creates the pin (the CYW43-hosted LED is unusable until
    the radio is powered up) and calls this. Sets LED_OFF so the LED starts
    from a known level rather than whatever it powered up as.
    """
    global led
    led = pin
    set_led_state(LED_OFF)


def set_led_state(state):
    """Record the steady LED state and render its resting level.

    The one place a steady level is set. Everything that wants to change what
    the LED means goes through here, so the indicator can never be left
    asserting the wrong thing, which is the bug G1 exists to kill.
    """
    global ledState
    ledState = state
    if led is None:
        return
    level = LED_STEADY_LEVEL.get(state, 0)
    try:
        if level:
            led.on()
        else:
            led.off()
    except Exception:
        # The LED is feedback, never a reason to fail an operation.
        pass


def led_toggle():
    """Flip the LED. Used by the connect loop for the connecting blink."""
    if led is None:
        return
    try:
        led.value(0 if led.value() else 1)
    except Exception:
        pass


def blink_led(num_blinks, on_ms=200, off_ms=200):
    """Blink a discrete count, then restore the steady state.

    Watchdog-safe: the waits go through wdt.sleep_fed, so a burst can never
    outlast the timeout the way a raw sleep could. Restoring the steady
    state afterwards means a blink never leaves the LED in the wrong
    resting level.
    """
    if led is None:
        return
    for _ in range(num_blinks):
        try:
            led.on()
        except Exception:
            pass
        wdt.sleep_fed(on_ms / 1000.0)
        try:
            led.off()
        except Exception:
            pass
        wdt.sleep_fed(off_ms / 1000.0)
    set_led_state(ledState)


def led_alert():
    """A brief double-blink for a delivered ring, then back to steady.

    Distinct from the connect confirmation (two quick flashes, not three
    slower ones) so the local feedback tells a ring apart from a reconnect.
    """
    blink_led(2, on_ms=80, off_ms=80)


# Kept as a thin alias: the fatal path and older call sites read more clearly
# as a plain blink count, and routing through blink_led makes even that
# watchdog-safe.
def blink_onboard_led(num_blinks):
    blink_led(num_blinks)
