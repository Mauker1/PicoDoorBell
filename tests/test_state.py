"""Exercise I2's persistence layer against a real filesystem.

Extracts just the state block from main.py -- it depends only on json and
os, so it runs unmodified under CPython. This is not F1's stub harness;
it only reaches the functions that have no hardware dependency.
"""
import ast
import os
import shutil
import tempfile

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'main.py')

WANT_FUNCS = {'default_state', 'migrate_state', 'state_is_future',
              'load_state', 'save_state', 'state_get', 'state_set'}
WANT_NAMES = {'STATE_PATH', 'STATE_TMP', 'STATE_VERSION', 'stateWritable'}

tree = ast.parse(open(SRC).read())
body = []
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in WANT_FUNCS:
        body.append(node)
    elif isinstance(node, ast.Assign):
        target = getattr(node.targets[0], 'id', '')
        if target in WANT_NAMES:
            body.append(node)

block = ast.Module(body=body, type_ignores=[])
code = compile(block, '<state>', 'exec')

logged = []


def fresh_ns():
    """Rebuild the module namespace, as if the board had just booted."""
    ns = {'json': __import__('json'), 'os': os,
          'append_to_log': lambda m: logged.append(m),
          'isinstance': isinstance, 'open': open, 'Exception': Exception,
          'OSError': OSError, 'ValueError': ValueError, 'True': True}
    exec(code, ns)
    return ns


def boot(ns):
    ns['state'] = ns['load_state']()
    return ns['state']


results = []


def check(label, got, expected):
    ok = got == expected
    results.append(ok)
    print('%-4s %-52s got=%r' % ('ok' if ok else 'FAIL', label, got))


work = tempfile.mkdtemp()
os.chdir(work)
print('working in', work)
print()

# --- 1. First boot, no state file -----------------------------------------
ns = fresh_ns()
st = boot(ns)
check('fresh boot returns defaults', st['v'], ns['STATE_VERSION'])
check('fresh boot chatId is None', st['chatId'], None)
check('fresh boot writes no file', os.path.exists('state.json'), False)

# --- 2. First real write ---------------------------------------------------
check('state_set reports a write', ns['state_set']('chatId', -100123), True)
check('file now exists', os.path.exists('state.json'), True)
check('write counter incremented', ns['state']['writes'], 1)
check('no temp file left behind', os.path.exists('state.json.tmp'), False)

# --- 3. Read-before-write: identical value must not erash flash ------------
before = ns['state']['writes']
check('setting identical value is a no-op', ns['state_set']('chatId', -100123), False)
check('write counter unchanged', ns['state']['writes'], before)

# --- 4. Changed value writes again -----------------------------------------
ns['state_set']('chatId', -100999)
check('changed value increments counter', ns['state']['writes'], 2)

# --- 5. Values survive a reboot --------------------------------------------
ns2 = fresh_ns()
st2 = boot(ns2)
check('chatId persisted across boot', st2['chatId'], -100999)
check('write counter persisted', st2['writes'], 2)
check('state_get reads through', ns2['state_get']('chatId'), -100999)
check('state_get fallback for absent key', ns2['state_get']('nope', 'dflt'), 'dflt')

# --- 6. Corrupt file --------------------------------------------------------
open('state.json', 'w').write('{this is not json')
ns3 = fresh_ns()
st3 = boot(ns3)
check('corrupt file falls back to defaults', st3['chatId'], None)
check('corrupt file leaves us writable', ns3['stateWritable'], True)
check('can still write after corruption', ns3['state_set']('chatId', 5), True)

# --- 7. File from newer firmware -------------------------------------------
open('state.json', 'w').write('{"v": 99, "chatId": -777, "secretNewField": 1}')
ns4 = fresh_ns()
st4 = boot(ns4)
check('newer file falls back to defaults', st4['chatId'], None)
check('newer file marks state read-only', ns4['stateWritable'], False)
check('save refuses while read-only', ns4['state_set']('chatId', 1), False)
raw = open('state.json').read()
check('newer file left untouched', 'secretNewField' in raw, True)

# --- 8. Stale temp file from a power cut mid-write --------------------------
open('state.json', 'w').write('{"v": 1, "chatId": -42, "epochAnchor": null, "writes": 3}')
open('state.json.tmp', 'w').write('{"v": 1, "chatId": -999')  # truncated
ns5 = fresh_ns()
st5 = boot(ns5)
check('stale temp file removed', os.path.exists('state.json.tmp'), False)
check('last good copy survives power cut', st5['chatId'], -42)

# --- 9. Older/partial file gets merged with defaults ------------------------
open('state.json', 'w').write('{"v": 1, "chatId": -1}')
ns6 = fresh_ns()
st6 = boot(ns6)
check('older file migrates to current version', st6['v'], ns6['STATE_VERSION'])
check('missing key filled from defaults', st6['epochAnchor'], None)
check('present key preserved', st6['chatId'], -1)
check('missing writes counter defaults to 0', st6['writes'], 0)

# --- 10. Non-dict payload ---------------------------------------------------
open('state.json', 'w').write('[1, 2, 3]')
ns7 = fresh_ns()
st7 = boot(ns7)
check('list payload rejected', st7['chatId'], None)
check('list payload stays writable', ns7['stateWritable'], True)
check('list payload is overwritable', ns7['state_set']('chatId', 7), True)
ns8 = fresh_ns()
st8 = boot(ns8)
check('overwrote the unusable file', st8['chatId'], 7)

print()
print('%d/%d passed' % (sum(results), len(results)))
shutil.rmtree(work, ignore_errors=True)
raise SystemExit(0 if all(results) else 1)
