"""Verify the log keeps recent entries and can actually be sent.

Two bugs, both seen on the bench during one outage.

The log was a single growing string. `log += entry` reallocates the whole
buffer every append, so a few hundred appends meant a few hundred
multi-kilobyte allocations on a 264 KB heap. TLS needs large contiguous
blocks, so the symptom is sends failing while small operations keep
working, which is what happened, and it correlated with the log filling.

It also refused new entries once full, preserving the *least* recent
events and printing a notice on every call, which buried the console and
discarded the errors that explained the failure.

Separately, /log sent the whole log in one message. Telegram caps a
message at 4096 characters while the log was allowed to reach 10000, so a
full log was an unconditional 400, and the old code cleared the log
anyway. Observed: /log worked early on and stopped once the log filled.

Imports main under the host-side stubs (F1).
"""
import ast
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', 'main.py')
# The buffer lives in applog since E2; main still sends it. Check both.
LOG_SOURCES = (SRC, os.path.join(HERE, '..', 'applog.py'))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, HERE)
import stubs

mem32 = stubs.mem32
WLAN = stubs.WLAN

results = []


def check(label, got, expected):
    ok = got == expected
    results.append(ok)
    print('%-4s %-54s got=%r' % ('ok' if ok else 'FAIL', label, got))


sent = []


def fresh():
    del sent[:]
    mem32.cells.clear()
    WLAN.connected = True
    m = stubs.load_firmware()
    m.telegram.send_message = make_sender(m)
    return m


def make_sender(m, fail=False):
    def send(chat, msg):
        if fail:
            return (m.net.REQUEST_RETRY, 0, None)
        sent.append(msg)
        return (m.net.REQUEST_OK, 200, {})
    return send


os.chdir(tempfile.mkdtemp())

# --- 1. Bounded by line count, keeping the newest --------------------------
m = fresh()
del m.applog.logLines[:]
for i in range(m.config.LOG_MAX_LINES + 50):
    m.applog.append_to_log('entry %d' % i)
check('log stops growing', len(m.applog.logLines), m.config.LOG_MAX_LINES)
check('the newest entry is kept',
      'entry %d' % (m.config.LOG_MAX_LINES + 49) in m.applog.logLines[-1], True)
check('the oldest is gone',
      any('entry 0 ' in line for line in m.applog.logLines), False)

# --- 2. An empty log says so -----------------------------------------------
m = fresh()
del m.applog.logLines[:]
m.telegram.print_log('chat')
check('an empty log is reported, not sent blank', sent[0], 'Log is empty.')

# --- 3. A short log goes in one message ------------------------------------
m = fresh()
del m.applog.logLines[:]
m.applog.append_to_log('hello')
m.telegram.print_log('chat')
check('one message for a short log', len(sent), 1)
check('log cleared after a successful send', len(m.applog.logLines), 1)

# --- 4. A long log is chunked under Telegram's limit -----------------------
# The regression: one 10000-character message is an unconditional 400.
m = fresh()
del m.applog.logLines[:]
for i in range(m.config.LOG_MAX_LINES):
    m.applog.append_to_log('a fairly long log line number %d, padded out %s' % (i, 'x' * 60))
total = len(m.applog.log_text())
check('the log exceeds a single message', total > 4096, True)

m.telegram.print_log('chat')
check('it was split across messages', len(sent) > 1, True)
check('every chunk fits Telegram',
      max(len(msg) for msg in sent) <= 4096, True)
check('nothing was dropped between chunks',
      sum(len(msg) for msg in sent) >= total - len(sent), True)
check('log cleared once it all landed', len(m.applog.logLines), 1)

# --- 5. A failed send keeps the log ----------------------------------------
# The old code cleared it regardless, destroying exactly what had failed to
# send.
m = fresh()
del m.applog.logLines[:]
for i in range(20):
    m.applog.append_to_log('entry %d' % i)
before = len(m.applog.logLines)
m.telegram.send_message = make_sender(m, fail=True)
m.telegram.print_log('chat')
# The failure itself is logged, so the count grows rather than resetting.
check('a log that failed to send is kept', len(m.applog.logLines) >= before, True)
check('and it was not reset to a single line', len(m.applog.logLines) > 1, True)

# --- 6. Appending does not rebuild the whole buffer ------------------------
# Structural, via AST so comments explaining the old bug do not match: no
# augmented assignment to a module-level log string anywhere.
concats = []
for path in LOG_SOURCES:
    tree = ast.parse(open(path).read())
    concats += [(os.path.basename(path), n.lineno) for n in ast.walk(tree)
                if isinstance(n, ast.AugAssign)
                and isinstance(n.target, (ast.Name, ast.Attribute))
                and getattr(n.target, 'id', getattr(n.target, 'attr', None))
                in ('log', 'logLines')]
check('no string concatenation onto the log', concats, [])

print()
print('%d/%d passed' % (sum(results), len(results)))
raise SystemExit(0 if all(results) else 1)
