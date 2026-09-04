# PicoDoorBell — Architecture

How the firmware works and why it is built this way.

Scope: this document describes **what exists**. `ROADMAP.md` describes what is
planned. The `README.md` is the build guide — hardware, wiring and Telegram
setup — and should stay free of internals.

---

## Target platform

| | Version | Notes |
| --- | --- | --- |
| Production unit | MicroPython v1.23.0 (2024-06-02), Pico W | Direct USB power, no battery |
| Bench unit | MicroPython v1.23.0, Pico W | Battery module on VSYS, jumper-switchable to direct USB |

Both boards run the same version. The bench unit was matched to production
rather than the reverse: production is the instrument for the reboot
investigation, and reflashing it would reset a baseline that has not yet been
collected.

v1.23.0 is deliberately behind the current release. See `ROADMAP.md` section K
for why, and for the `machine.mem_backup()` conflict to check before any
upgrade.

The `ujson`/`uos` import fallbacks stay regardless. v1.23.0 provides both names,
but the project targets other people's boards too, and MicroPython only exposed
the u-prefixed names before v1.20.

---

## Design principle

The failure mode this firmware exists to avoid is **silent failure**: a device
that boots, runs, looks healthy, and quietly stops notifying. A doorbell that
loudly refuses to start is a nuisance; a doorbell that appears fine and drops
presses is worse, because nobody investigates.

Every design decision below follows from that. Where there is a choice between
failing loudly and degrading quietly, this codebase fails loudly.

---

## State tiers

Persisted values are split into four tiers by **how often they are written**.
The tier is not a suggestion — it determines whether a value may touch flash at
all, and getting it wrong destroys hardware.

| Tier | Medium | Survives | Cost per write | Contents |
| --- | --- | --- | --- | --- |
| **0** | RAM | nothing | free | Rolling log, alert counters, live event queue |
| **1** | Watchdog scratch registers | warm reset (**not** power loss) | free | Magic word, boot count |
| **2** | Flash — `state.json` | everything | one 4 KB sector erase | `chatId`, `epochAnchor`, `writes` |
| **3** | Flash — `state.json` | everything | one 4 KB sector erase | Queue snapshot, written only on rare triggers |

### The rule

> **Never write flash on a timer. Only on a real state transition.**

Everything else in this section is a consequence of that one rule.

### Why the tiers exist

Flash endurance on the Pico W is about **write frequency, not write size**. The
board carries 2 MB of QSPI NOR (W25Q16-class) with 4 KB erase sectors and
roughly 100,000 program/erase cycles per sector. A 40-byte state file and a
4 KB one cost exactly the same, because the erase granularity is a sector
either way.

Pessimistic floor, assuming no wear levelling and every write landing on the
same sector:

| Write pattern | Sector lifetime |
| --- | --- |
| 1 write per press, 20 presses/day | ~13 years |
| 10 writes/day | ~27 years |
| **1 write/minute** | **~69 days** |
| 1 write/second | ~28 hours |

Event-driven writes are free. Scheduled writes are lethal. There is no middle
ground worth reasoning about, which is why the rule is absolute rather than a
budget.

Real figures should be better than the table: MicroPython's RP2 port uses
littlefs2, which does *dynamic* wear levelling — allocating from a rotating
pointer across free blocks rather than rewriting in place. Treat that as
headroom, not permission. littlefs does not do *static* levelling, so blocks
holding rarely-changed data never rotate into the pool and the metadata pair
stays comparatively hot.

### Why this matters more than the price of a Pico

Worn NOR flash does not fail cleanly. It begins failing **writes** while reads
still succeed. The result is a device that boots, runs, looks healthy, and
silently stops persisting state — the exact failure class this firmware is
built to eliminate. Replacing a cheap board is easy; noticing you need to is
not.

### Choosing a tier for a new value

Ask, in order:

1. **Does it need to survive anything?** No → **Tier 0**. This is the default
   and most values belong here. The log, counters and the live queue are all
   Tier 0.
2. **Does it only need to survive a reset, not a power cut?** → **Tier 1**.
   Free, zero wear, 32 bytes total.
3. **Does it change rarely — a handful of times in the device's life?** →
   **Tier 2**. A chat ID that changes on supergroup migration qualifies. A
   "last doorbell press" timestamp does **not**; it would be one write per
   press forever.
4. **Does it change often but only needs persisting under rare conditions?** →
   **Tier 3**. The event queue lives in RAM and is snapshotted only when an
   outage runs long or a deliberate reset is imminent.

If a value seems to need Tier 2 but changes on a schedule, the answer is not a
faster flash write — it is to keep it in RAM and persist only the transition
that actually matters.

---

## Tier 2 — `state.json`

### Schema

```json
{
  "v": 1,
  "chatId": null,
  "epochAnchor": null,
  "writes": 0
}
```

| Field | Purpose |
| --- | --- |
| `v` | Schema version. Drives migration and downgrade safety. |
| `chatId` | Overrides the configured chat after a supergroup migration. |
| `epochAnchor` | Wall-clock epoch captured at the last NTP sync. |
| `writes` | Monotonic write counter. Makes wear observable rather than theoretical. |

### API

```python
state_get(key, fallback=None)   # read from the in-RAM mirror
state_set(key, value)           # write only if the value actually changed
```

`state_set` compares against the in-RAM mirror before writing. Because that
mirror is loaded from flash and only mutated through `state_set`, the
comparison **is** the read-before-write check. Setting an unchanged value costs
no erase, so callers may set defensively — `state_set('chatId', x)` on every
API error is safe.

### Atomicity

Writes go to `state.json.tmp`, then `os.rename()` over the target. Rename is
atomic under littlefs, so a power cut leaves either the old copy or the new
one, never a half-written file.

A stale `state.json.tmp` found at boot means power was lost mid-write. The
rename never happened, so `state.json` is still the last good copy; the scrap
is discarded and logged.

> **Caveat:** this guarantee is littlefs-specific. If the build turns out to use
> FAT, rename is not power-fail atomic. Verifying the filesystem is an open item
> in `ROADMAP.md` (I4).

### Failure policy

| Situation | Result | Writes | Reasoning |
| --- | --- | --- | --- |
| No file | Defaults | enabled | First boot |
| Invalid JSON | Defaults | enabled | Garbage carries nothing worth protecting |
| Valid JSON, wrong shape | Defaults | enabled | Same — nothing to preserve |
| `v` > `STATE_VERSION` | Defaults | **disabled** | Written by newer firmware after a rollback; the data may matter |
| `v` ≤ `STATE_VERSION` | Migrated | enabled | Missing keys filled from defaults |

Read-only mode exists for exactly one case: a rollback must not clobber state
belonging to a newer build. Everything else stays writable so a single bad file
cannot permanently wedge persistence.

### Adding a field

1. Add it to `default_state()` with a safe default.
2. Bump `STATE_VERSION`.
3. Add a migration step in `migrate_state()`, oldest first.

Fields absent from an older file are filled from defaults automatically, so a
purely additive change often needs no migration body — but still bump the
version, so a downgraded build recognises the file as newer and leaves it
alone.

---

## Boot sequence

`boot()` runs three stages in a fixed order. Only the first is fatal.

| Stage | Function | On failure |
| --- | --- | --- |
| 0. Diagnosis | `read_reset_info()` | Cannot fail; every read is guarded |
| 1. Hardware | `setup_hardware()` | `error_halt()` — fast LED blink, forever |
| 2. Flash | `load_state()` | Log, fall back to defaults, continue |
| 3. Network | `connect_wifi()` + `announce_startup()` | Log, continue; the main loop retries |

### Why the order matters

Previously `connect_wifi()` ran at module scope, outside any `try`, and sent the
startup message immediately after association — precisely when DNS is least
likely to be ready. If that send raised, the script died before
`machine.Pin(16)` was ever reached. The doorbell input was never configured and
the device was dead until someone power-cycled it, with no watchdog to notice.

Configuring pins first means a network problem can never stop the board from
watching the button. Once B1 lands, presses arriving during a network outage
are latched by the IRQ and delivered when the link returns.

### Fatal versus recoverable

Only stage 1 is fatal, and in practice it fails only on a bad pin number — a
configuration error a human must fix. `error_halt()` therefore blinks rather
than resetting: a reset loop would hide the fault. When B2 adds the watchdog,
this becomes a deliberate escalation point.

Stages 2 and 3 are recoverable by construction. Missing or unusable state falls
back to defaults; missing network is the main loop's problem.

### `wlan.active(True)` lives in `setup_hardware()`

Not an accident of ordering. On the Pico W the onboard LED hangs off the CYW43
wireless chip, so `machine.Pin('LED')` is unusable until the interface is
powered up. Hardware setup therefore has to activate the interface even though
connecting is a later stage.

### Connection and notification are separate

`connect_wifi()` connects. `announce_startup()` notifies. They used to be one
function, which meant a transport failure looked like a connection failure and
took the boot down with it. Callers now decide whether to announce — the main
loop does so after a reconnect, `boot()` after the first connect.

---



All network calls go through one function. This is an enforced invariant, not a
convention: `requests.*` appears nowhere else in the codebase, and the response
object never escapes `do_request`.

```python
outcome, status, body = do_request(method, url, payload=None)
```

`do_request` never raises for network or HTTP-level failures. Callers branch on
`outcome` instead of unwinding.

### Outcomes

| Outcome | Meaning | Caller should |
| --- | --- | --- |
| `REQUEST_OK` | 2xx, body decoded | proceed |
| `REQUEST_RETRY` | Network fault, 5xx, or no response at all | retry with backoff |
| `REQUEST_RATE_LIMIT` | 429 | wait `retry_after(body)` seconds, then retry |
| `REQUEST_FATAL` | Other 4xx | log and give up; retrying will not help |

### Why this shape

`urequests` **does not raise on 4xx** — it returns a response object carrying
the error status. Before this layer existed, every API failure was invisible:
the firmware printed `Doorbell pressed!` and carried on believing it had
delivered the notification. Status classification is what turns that into an
observable event.

The second problem was socket lifetime. `response.close()` sat after the parse
loop, outside any guard, so any exception while parsing leaked the socket. That
is the classic MicroPython + `urequests` slow death: works for days, then
`ENOMEM`. `do_request` closes in a `finally`, unconditionally, and runs
`gc.collect()` after every request.

The body is decoded **while the socket is still open**, then the response is
released. Telegram answers JSON for errors too, so this is also how error
details are read.

---

## On-device files

| File | Purpose |
| --- | --- |
| `main.py` | Firmware |
| `secrets.py` | Credentials and chat configuration. User-edited, never written by the firmware. |
| `state.json` | Tier 2 runtime state. Firmware-written, never user-edited. |
| `state.json.tmp` | Transient. Present only mid-write, or as a crash remnant. |

`secrets.py` and `state.json` are deliberately separate. Runtime state must
never be written into a file the user also edits by hand — that invites a merge
conflict on a microcontroller, and makes credential rotation destructive.

---

## Testing

`tests/test_state.py` exercises the persistence layer against a real
filesystem: 30 assertions covering first boot, write suppression, reboot
persistence, corrupt and non-dict payloads, newer-version files, stale temp
files and partial schemas. It runs under CPython with no board attached.

`tests/test_reset.py` covers reset-cause capture: cold versus warm boot, the
counter surviving a warm reset and restarting after a power cut, watchdog
identification, unknown cause codes, and — most importantly — that a brownout
and RUN-pin pickup produce visibly different output. 27 assertions.

`tests/test_boot.py` runs `main.py` under stub hardware modules and asserts the
boot ordering: that the doorbell pin is configured before any network call, and
that a failing startup send no longer prevents it. The baseline firmware fails
this test — it dies with `OSError` and never reaches `machine.Pin(16)`.

Both files work by extracting code from `main.py` via AST and running it in a
synthetic namespace — possible only because those functions depend on
nothing but `json` and `os`, and because the trailing `while True:` loop can be
stripped from the tree before executing it. **This is temporary scaffolding.**
Once the code is split into modules, both files should be rewritten as plain
imports.