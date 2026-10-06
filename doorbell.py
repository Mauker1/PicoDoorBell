####################################################################################
# Latched inputs: records, IRQ handlers, pulse judging (E2).
#
# Records are plain lists, allocated once at setup. Interrupt handlers may not
# allocate under MicroPython, and assigning to an existing list slot does not.
# The list-of-records shape is also what lets a second input (G9, telling the
# building entrance from the flat door) become another entry rather than a
# rewrite -- though only one is wired today.
#
# Dependencies: config and applog. The two things this module needs from the
# layers around it are passed in rather than imported:
#
#   on_ring(width)   an accepted ring. Set by main at import to
#                    ringqueue.enqueue_ring, so input judging never pulls in
#                    the queue, persist or the network stack. Starts as None
#                    so an unwired hook fails loudly.
#   pass window      prevPassTicks and lastPassTicks are main-loop timing,
#                    owned by the loop; poll_inputs() takes them per call to
#                    judge whether a ring fell where polling could not see it.
####################################################################################

import machine
import time
import config
import applog

IN_NAME = 0        # for log lines
IN_PIN = 1         # machine.Pin
IN_RISE = 2        # ticks_ms of the pending rising edge
IN_FALL = 3        # ticks_ms of the falling edge that closed it
IN_PENDING = 4     # a rising edge is waiting to be processed
IN_COMPLETE = 5    # its falling edge has arrived, so the width is known
IN_LAST_EDGE = 6   # debounce reference
IN_LAST_ALERT = 7  # lockout reference
IN_RINGS = 8       # accepted
IN_REJECTED = 9    # too short to be real
IN_MISSED = 10     # would have been lost by the old polling loop
IN_FIELDS = 11

# Hook into the layer above, set by main at import. See the header.
on_ring = None

inputs = []

def make_edge_handler(entry):
    """Build the interrupt handler for one input.

    The closure is created once at setup; calling it allocates nothing,
    which is the requirement for a MicroPython ISR. It does no I/O, no
    string work and no logging -- it stamps two integers and returns.

    Both edges are watched deliberately. Capturing the falling edge in
    hardware means the pulse width is known exactly, even if the main loop
    was blocked in a TLS handshake for several seconds and only gets to
    look afterwards. Validating by re-reading the pin instead would have
    failed in precisely that case: the pulse would be long over, the pin
    back at ground, and a real ring discarded as noise.
    """
    def handler(pin):
        now = time.ticks_ms()
        if pin.value() == config.pressed:
            if (entry[IN_COMPLETE] and
                    time.ticks_diff(now, entry[IN_FALL]) < config.DEBOUNCE_MS):
                # The line came back up almost immediately, so the fall was
                # contact chatter on the way in, not the release. Reopen the
                # pulse and keep the original rise time.
                entry[IN_COMPLETE] = False
                entry[IN_LAST_EDGE] = now
                return
            if entry[IN_PENDING] and not entry[IN_COMPLETE]:
                # Already mid-pulse; nothing to do but note the edge.
                entry[IN_LAST_EDGE] = now
                return
            entry[IN_LAST_EDGE] = now
            entry[IN_RISE] = now
            entry[IN_COMPLETE] = False
            entry[IN_PENDING] = True
        else:
            entry[IN_LAST_EDGE] = now
            if entry[IN_PENDING] and not entry[IN_COMPLETE]:
                # Provisional. process_input() waits a debounce window
                # before trusting it, and a rise inside that window undoes
                # it.
                entry[IN_FALL] = now
                entry[IN_COMPLETE] = True
    return handler

def add_input(name, pinNumber):
    """Register a latched input and arm its interrupt."""
    pin = machine.Pin(pinNumber, machine.Pin.IN, machine.Pin.PULL_DOWN)
    entry = [0] * IN_FIELDS
    entry[IN_NAME] = name
    entry[IN_PIN] = pin
    entry[IN_PENDING] = False
    entry[IN_COMPLETE] = False
    inputs.append(entry)
    pin.irq(handler=make_edge_handler(entry),
            trigger=machine.Pin.IRQ_RISING | machine.Pin.IRQ_FALLING)
    print(name + ' input ready on GP' + str(pinNumber))
    return entry

def was_unpollable(entry, prevPassTicks, lastPassTicks):
    """Would the old polling loop have missed this ring?

    True when the whole pulse fell between two passes of the main loop, so
    no poll could have observed it. Counted rather than acted upon: it
    turns 'we might have been dropping rings' into a number.
    """
    if not entry[IN_COMPLETE]:
        return False
    return (time.ticks_diff(entry[IN_RISE], prevPassTicks) > 0 and
            time.ticks_diff(lastPassTicks, entry[IN_FALL]) > 0)

def process_input(entry, prevPassTicks, lastPassTicks):
    """Decide what a latched edge was, and act on it."""
    now = time.ticks_ms()
    width = None
    if entry[IN_COMPLETE]:
        if time.ticks_diff(now, entry[IN_FALL]) < config.DEBOUNCE_MS:
            # The close is still provisional: the line may yet bounce back
            # up and prove this was chatter rather than the release.
            return
        width = time.ticks_diff(entry[IN_FALL], entry[IN_RISE])
    elif time.ticks_diff(now, entry[IN_RISE]) > config.STUCK_INPUT_MS:
        # Still high long after any real ring would have ended.
        entry[IN_PENDING] = False
        applog.report(entry[IN_NAME] + ' input stuck high')
        return
    else:
        # Mid-pulse. Leave it latched and look again next pass.
        return

    entry[IN_PENDING] = False

    if width < config.MIN_PULSE_MS:
        entry[IN_REJECTED] += 1
        applog.report(entry[IN_NAME] + ' transient ignored, ' + str(width) + 'ms')
        return

    if was_unpollable(entry, prevPassTicks, lastPassTicks):
        entry[IN_MISSED] += 1

    if (entry[IN_RINGS] > 0 and
            time.ticks_diff(now, entry[IN_LAST_ALERT]) < config.ALERT_LOCKOUT_MS):
        # Same ring, or an impatient second press. One alert is enough.
        return

    entry[IN_RINGS] += 1
    entry[IN_LAST_ALERT] = now
    applog.report(entry[IN_NAME] + ' ring, ' + str(width) + 'ms')
    # Queued rather than sent. Delivery is the queue's job, and a ring is
    # not discarded until Telegram confirms it.
    on_ring(width)

def poll_inputs(prevPassTicks, lastPassTicks):
    """Judge every latched input against this main-loop pass window."""
    for entry in inputs:
        if entry[IN_PENDING]:
            process_input(entry, prevPassTicks, lastPassTicks)

def summary_parts():
    """One part per input, for the heartbeat (C3); main appends the queue."""
    parts = []
    for entry in inputs:
        parts.append(entry[IN_NAME] + ': ' + str(entry[IN_RINGS]) +
                     ' rings, ' + str(entry[IN_REJECTED]) +
                     ' transients, ' + str(entry[IN_MISSED]) +
                     ' unpollable')
    return parts
