"""Verify the log keeps recent entries and can actually be sent.

Two bugs, both seen on the bench during one outage.

The log was a single growing string. `log += entry` reallocates the whole
buffer every append, so a few hundred appends meant a few hundred
multi-kilobyte allocations on a 264 KB heap. TLS needs large contiguous
blocks, so the symptom is sends failing while small operations keep
working -- which is what happened, and it correlated with the log filling.

It also refused new entries once full, preserving the *least* recent
events and printing a notice on every call, which buried the console and
discarded the errors that explained the failure.

Separately, /log sent the whole log in one message. Telegram caps a
message at 4096 characters while the log was allowed to reach 10000, so a
full log was an unconditional 400 -- and the old code cleared the log
anyway. Observed: /log worked early on and stopped once the log filled.

Reuses the stub hardware from test_boot.py.
"""
import ast
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, '..', 'main.py')

_src = open(os.path.join(HERE, 'test_boot.py')).read()
_stubs = {'__name__': 'stubs', '__file__': os.path.join(HERE, 'test_boot.py')}
exec(compile(_src[:_src.index('work = tempfile.mkdtemp()')], 'stubs', 'exec'), _stubs)
_stubs['install_stubs']()

mem32 = _stubs['mem32']
WLAN = _stubs['WLAN']

results = []


def check(label, got, expected):
    ok = got == expected
    results.append(ok)
    print('%-4s %-54s got=%r' % ('ok' if ok else 'FAIL', label, got))


def load_firmware():
    tree = ast.parse(open(SRC).read())
    tree.body = [n for n in tree.body if not isinstance(n, ast.While)]
    ns = {'__name__': 'main'}
    exec(compile(tree, 'main.py', 'exec'), ns)
    return ns


sent = []


def fresh():
    del sent[:]
    mem32.cells.clear()
    WLAN.connected = True
    ns = load_firmware()
    ns['send_message'] = make_sender(ns)
    return ns


def make_sender(ns, fail=False):
    def send(chat, msg):
        if fail:
            return (ns['REQUEST_RETRY'], 0, None)
        sent.append(msg)
        return (ns['REQUEST_OK'], 200, {})
    return send


os.chdir(tempfile.mkdtemp())

# --- 1. Bounded by line count, keeping the newest --------------------------
ns = fresh()
del ns['logLines'][:]
for i in range(ns['LOG_MAX_LINES'] + 50):
    ns['append_to_log']('entry %d' % i)
check('log stops growing', len(ns['logLines']), ns['LOG_MAX_LINES'])
check('the newest entry is kept',
      'entry %d' % (ns['LOG_MAX_LINES'] + 49) in ns['logLines'][-1], True)
check('the oldest is gone',
      any('entry 0 ' in line for line in ns['logLines']), False)

# --- 2. An empty log says so -----------------------------------------------
ns = fresh()
del ns['logLines'][:]
ns['print_log']('chat')
check('an empty log is reported, not sent blank', sent[0], 'Log is empty.')

# --- 3. A short log goes in one message ------------------------------------
ns = fresh()
del ns['logLines'][:]
ns['append_to_log']('hello')
ns['print_log']('chat')
check('one message for a short log', len(sent), 1)
check('log cleared after a successful send', len(ns['logLines']), 1)

# --- 4. A long log is chunked under Telegram's limit -----------------------
# The regression: one 10000-character message is an unconditional 400.
ns = fresh()
del ns['logLines'][:]
for i in range(ns['LOG_MAX_LINES']):
    ns['append_to_log']('a fairly long log line number %d, padded out %s' % (i, 'x' * 60))
total = len(ns['log_text']())
check('the log exceeds a single message', total > 4096, True)

ns['print_log']('chat')
check('it was split across messages', len(sent) > 1, True)
check('every chunk fits Telegram',
      max(len(m) for m in sent) <= 4096, True)
check('nothing was dropped between chunks',
      sum(len(m) for m in sent) >= total - len(sent), True)
check('log cleared once it all landed', len(ns['logLines']), 1)

# --- 5. A failed send keeps the log ----------------------------------------
# The old code cleared it regardless, destroying exactly what had failed to
# send.
ns = fresh()
del ns['logLines'][:]
for i in range(20):
    ns['append_to_log']('entry %d' % i)
before = len(ns['logLines'])
ns['send_message'] = make_sender(ns, fail=True)
ns['print_log']('chat')
# The failure itself is logged, so the count grows rather than resetting.
check('a log that failed to send is kept', len(ns['logLines']) >= before, True)
check('and it was not reset to a single line', len(ns['logLines']) > 1, True)

# --- 6. Appending does not rebuild the whole buffer ------------------------
# Structural, via AST so comments explaining the old bug do not match: no
# augmented assignment to a module-level log string anywhere.
tree = ast.parse(open(SRC).read())
concats = [n.lineno for n in ast.walk(tree)
           if isinstance(n, ast.AugAssign)
           and isinstance(n.target, ast.Name)
           and n.target.id in ('log', 'logLines')]
check('no string concatenation onto the log', concats, [])

print()
print('%d/%d passed' % (sum(results), len(results)))
raise SystemExit(0 if all(results) else 1)
