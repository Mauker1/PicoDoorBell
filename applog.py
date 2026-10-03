####################################################################################
# C2 in-memory log (E2).
#
# The log is a bounded list of timestamped lines in RAM. It is Tier 0: never
# persisted, lost on reset, and that is by design, since writing it to flash on
# every event would break the one rule that matters for wear.
#
# This module is the mechanism: the buffer, its bound, the timestamp prefix,
# and the append and report primitives. It does not send. Delivering the log
# over Telegram (/log, chunked under the message limit, cleared only once every
# piece has landed) is the caller's policy and stays with the network side;
# otherwise the dependency would point upward and make a cycle. For the same
# reason it reads nothing from main: the seed line for a cleared log is passed
# to reset_log(), and the display offset is set once by main at import.
#
# Dependencies: config (the bound) and clockmod (wall-clock reads), both
# leaves, plus time for the ticks fallback.
####################################################################################

import time
import config
import clockmod

# Kept as a list of lines, never as one growing string. `log += entry`
# reallocates the whole buffer on every append; a few hundred appends during
# an outage means a few hundred multi-kilobyte allocations and frees, which
# fragments a 264 KB heap. TLS handshakes need large contiguous blocks, so
# the symptom is sends beginning to fail while everything small still works.
logLines = []

# Display offset for timestamps, in seconds. main owns the value (read from
# board.py, UTC by default) and sets it here once at import, so this module
# never reaches up into main or out to board.
utcOffset = 0

def log_prefix():
    """Timestamp prefix for a log line.

    A real wall-clock time once C1 has synced; otherwise the ticks_ms value
    tagged with a leading 't' so a relative stamp can never be mistaken for
    an absolute one. This is the roadmap's "mark those entries as such": a
    log spanning a sync shows exactly where real time began.
    """
    epoch = clockmod.clock_now()
    if epoch is not None:
        return clockmod.format_timestamp(epoch, utcOffset)
    return 't' + str(time.ticks_ms())

def append_to_log(message):
    """Append, dropping the oldest lines once full.

    Previously this refused new entries at the limit, which preserved the
    *least* recent events and printed a full-log notice on every call. A
    long outage therefore filled the log with the start of the outage,
    discarded the errors that explained it, and buried the console in
    notices. Exactly backwards for diagnosis.
    """
    logLines.append(log_prefix() + ' ' + message)
    while len(logLines) > config.LOG_MAX_LINES:
        # Drop the oldest. Refusing new entries instead, as this once did,
        # preserves the least recent events -- backwards for diagnosis.
        logLines.pop(0)

def report(message):
    """Log it and say it.

    append_to_log() alone writes to a buffer nobody is watching. Several
    events that only matter while someone is looking -- ring widths,
    rejected transients, the stability write -- were invisible on the
    console because of that.
    """
    print(message)
    append_to_log(message)

def log_text():
    return '\n'.join(logLines)

def reset_log(header):
    """Clear the log and seed it with one line of context.

    The caller supplies the line (today, the current WiFi status), so a
    freshly cleared log still says what state the device is in, without
    this module reading anything from main.
    """
    del logLines[:]
    logLines.append(log_prefix() + ' ' + header)
