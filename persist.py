####################################################################################
# Tier 2 persistence: state.json (E2).
#
# Flash endurance is about write *frequency*, not size: a 40 byte file and a
# 4 KB file both cost one 4 KB sector erase. At ~100k cycles per sector, a
# handful of writes per device lifetime is free and one write per minute
# destroys a sector in about 69 days.
#
# THE RULE: never write flash on a timer. Only on a real state transition.
#
# One deliberate exception: each NTP resync (every NTP_RESYNC_MS, twelve
# hours) refreshes the coarse epochAnchor copy. Bounded at two writes a day,
# it is negligible for wear and keeps restored ring aging fresh.
#
# Tier 0 (log, counters, live queue) stays in RAM and is never persisted.
# Tier 1 (reset reason, boot count) belongs in the watchdog scratch registers.
# Tier 3 (queue snapshots) reuses this file but writes only on rare triggers.
#
# This module owns the in-RAM mirror (state) and is the only code that ever
# rebinds it: boot calls load(), everything else goes through state_get and
# state_set. Its one dependency is applog, for reporting what it found.
####################################################################################

# MicroPython exposed these only as ujson/uos before v1.20; the fallback
# keeps the file working on older builds and under host-side CPython.
try:
    import ujson as json
except ImportError:
    import json
try:
    import uos as os
except ImportError:
    import os
import applog

STATE_PATH = 'state.json'
STATE_TMP = 'state.json.tmp'
STATE_VERSION = 3

# Set False when the on-disk file was written by newer firmware, so a
# downgraded build runs on defaults instead of clobbering it.
stateWritable = True

def default_state():
    # Built fresh each call -- a module-level dict literal would be shared
    # and mutated by reference.
    return {
        'v': STATE_VERSION,
        'chatId': None,        # A4: overrides secrets on supergroup migration
        'epochAnchor': None,   # C1: NTP epoch captured at last sync
        'writes': 0,           # I3: wear counter, reported in the heartbeat
        'boots': 0,            # C5: stable boots; survives hardware resets
        'queue': [],           # B6: rings not yet delivered (Tier 3)
    }

def state_is_future(data):
    """True if the file was written by firmware newer than this build."""
    return isinstance(data, dict) and data.get('v', 0) > STATE_VERSION

def migrate_state(data):
    """Upgrade a decoded state file to STATE_VERSION.

    Returns the migrated dict, or None if the shape is unusable. Callers
    treat None as 'no usable state', not as 'do not touch this file' --
    see state_is_future() for that case.
    """
    if not isinstance(data, dict):
        return None
    version = data.get('v', 0)
    # Migrations chain here, oldest first. v1 -> v2 added 'boots' and
    # v2 -> v3 added 'queue'; neither needs a body, because the merge below
    # fills absent keys from defaults. The version bumps still matter, so a
    # downgraded build recognises the file as newer and leaves it alone.
    # Later migrations that do need a body go here:
    #   if version < 4:
    #       data['newField'] = derive_from(data)
    #       version = 4
    merged = default_state()
    for key in merged:
        if key in data:
            merged[key] = data[key]
    merged['v'] = STATE_VERSION
    return merged

def load_state():
    """Read Tier 2 state from flash. Always returns a usable dict."""
    global stateWritable
    # A leftover temp file means we lost power mid-write. The rename never
    # happened, so state.json is still the last good copy; drop the scrap.
    try:
        os.remove(STATE_TMP)
        applog.append_to_log('Discarded stale ' + STATE_TMP)
    except OSError:
        pass

    raw = None
    try:
        f = open(STATE_PATH, 'r')
        try:
            raw = json.load(f)
        finally:
            f.close()
    except OSError:
        # No state file: first boot, or nothing has ever needed persisting.
        return default_state()
    except ValueError:
        applog.append_to_log('State file corrupt, falling back to defaults')
        return default_state()

    if state_is_future(raw):
        # Written by newer firmware. Do not guess at its schema and do not
        # overwrite it -- the user may simply have rolled back.
        stateWritable = False
        applog.append_to_log('State file is newer than firmware; running read-only')
        return default_state()

    migrated = migrate_state(raw)
    if migrated is None:
        # Structurally wrong but syntactically valid, e.g. a bare list.
        # Nothing meaningful to preserve, so stay writable and let the
        # next real transition overwrite it.
        applog.append_to_log('State file unusable, falling back to defaults')
        return default_state()
    return migrated

def save_state():
    """Write Tier 2 state atomically. Returns True on success.

    Writes to a temp file and renames over the target. Rename is atomic
    under littlefs, so a power cut can never leave a half-written state
    file -- it leaves either the old copy or the new one.
    """
    global state
    if not stateWritable:
        return False
    state['writes'] = state.get('writes', 0) + 1
    payload = json.dumps(state)
    f = None
    try:
        f = open(STATE_TMP, 'w')
        f.write(payload)
        f.close()
        f = None
        os.rename(STATE_TMP, STATE_PATH)
        return True
    except OSError as e:
        applog.append_to_log('State save failed: ' + str(e))
        if f is not None:
            try:
                f.close()
            except Exception:
                pass
        try:
            os.remove(STATE_TMP)
        except OSError:
            pass
        return False

def state_get(key, fallback=None):
    return state.get(key, fallback)

def state_set(key, value):
    """Set a Tier 2 value, writing flash only if it actually changed.

    The in-RAM dict mirrors what is on flash, so comparing here is the
    read-before-write check -- an unchanged value costs no erase.
    """
    global state
    if state.get(key, None) == value:
        return False
    state[key] = value
    return save_state()

# Populated for real by load(), called from boot(). Defined here so
# state_get/state_set always have a dict to work with, without touching
# flash at import time.
state = default_state()

def load():
    """Load Tier 2 state from flash into the in-RAM mirror.

    The only place state is rebound. load_state() already falls back to
    defaults for a missing, corrupt or future file; this also covers any
    unexpected failure, so boot always ends up with a usable dict.
    """
    global state
    try:
        state = load_state()
    except Exception as e:
        state = default_state()
        applog.report('State load failed, using defaults: ' + str(e))
