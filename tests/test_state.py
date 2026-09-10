"""Exercise I2's persistence layer against a real filesystem.

Imports main under the host-side stubs (F1) and drives the state functions
directly. Previously this suite AST-extracted just the state block to avoid
main.py's hardware imports; the stubs make that unnecessary, so it now loads
the firmware the same way every other suite does. The functions under test
(load_state, save_state, state_set and friends) depend only on json and os,
so nothing here touches the fakes beyond the import itself.
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))   # for import main
sys.path.insert(0, HERE)                        # for import stubs
import stubs

results = []


def check(label, got, expected):
    ok = got == expected
    results.append(ok)
    print('%-4s %-52s got=%r' % ('ok' if ok else 'FAIL', label, got))


def boot():
    """Fresh import (a reboot) followed by a state load.

    load_firmware() drops the cached module and re-imports, so every call
    starts main with its globals at their initial values, which is what a
    reboot is. main.boot() is not used here: it does hardware and network
    work this suite has no interest in. The one thing a boot does that
    matters to state is load_state(), so that is called directly.
    """
    m = stubs.load_firmware()
    m.state = m.load_state()
    return m


work = tempfile.mkdtemp()
os.chdir(work)
print('working in', work)
print()

# --- 1. First boot, no state file -----------------------------------------
m = boot()
check('fresh boot returns defaults', m.state['v'], m.STATE_VERSION)
check('fresh boot chatId is None', m.state['chatId'], None)
check('fresh boot writes no file', os.path.exists('state.json'), False)

# --- 2. First real write ---------------------------------------------------
check('state_set reports a write', m.state_set('chatId', -100123), True)
check('file now exists', os.path.exists('state.json'), True)
check('write counter incremented', m.state['writes'], 1)
check('no temp file left behind', os.path.exists('state.json.tmp'), False)

# --- 3. Read-before-write: identical value must not erase flash ------------
before = m.state['writes']
check('setting identical value is a no-op', m.state_set('chatId', -100123), False)
check('write counter unchanged', m.state['writes'], before)

# --- 4. Changed value writes again -----------------------------------------
m.state_set('chatId', -100999)
check('changed value increments counter', m.state['writes'], 2)

# --- 5. Values survive a reboot --------------------------------------------
m2 = boot()
check('chatId persisted across boot', m2.state['chatId'], -100999)
check('write counter persisted', m2.state['writes'], 2)
check('state_get reads through', m2.state_get('chatId'), -100999)
check('state_get fallback for absent key', m2.state_get('nope', 'dflt'), 'dflt')

# --- 6. Corrupt file --------------------------------------------------------
open('state.json', 'w').write('{this is not json')
m3 = boot()
check('corrupt file falls back to defaults', m3.state['chatId'], None)
check('corrupt file leaves us writable', m3.stateWritable, True)
check('can still write after corruption', m3.state_set('chatId', 5), True)

# --- 7. File from newer firmware -------------------------------------------
open('state.json', 'w').write('{"v": 99, "chatId": -777, "secretNewField": 1}')
m4 = boot()
check('newer file falls back to defaults', m4.state['chatId'], None)
check('newer file marks state read-only', m4.stateWritable, False)
check('save refuses while read-only', m4.state_set('chatId', 1), False)
raw = open('state.json').read()
check('newer file left untouched', 'secretNewField' in raw, True)

# --- 8. Stale temp file from a power cut mid-write --------------------------
open('state.json', 'w').write('{"v": 1, "chatId": -42, "epochAnchor": null, "writes": 3}')
open('state.json.tmp', 'w').write('{"v": 1, "chatId": -999')  # truncated
m5 = boot()
check('stale temp file removed', os.path.exists('state.json.tmp'), False)
check('last good copy survives power cut', m5.state['chatId'], -42)

# --- 9. Older/partial file gets merged with defaults ------------------------
open('state.json', 'w').write('{"v": 1, "chatId": -1}')
m6 = boot()
check('older file migrates to current version', m6.state['v'], m6.STATE_VERSION)
check('missing key filled from defaults', m6.state['epochAnchor'], None)
check('present key preserved', m6.state['chatId'], -1)
check('missing writes counter defaults to 0', m6.state['writes'], 0)

# --- 10. Non-dict payload ---------------------------------------------------
open('state.json', 'w').write('[1, 2, 3]')
m7 = boot()
check('list payload rejected', m7.state['chatId'], None)
check('list payload stays writable', m7.stateWritable, True)
check('list payload is overwritable', m7.state_set('chatId', 7), True)
m8 = boot()
check('overwrote the unusable file', m8.state['chatId'], 7)

print()
print('%d/%d passed' % (sum(results), len(results)))
shutil.rmtree(work, ignore_errors=True)
raise SystemExit(0 if all(results) else 1)
