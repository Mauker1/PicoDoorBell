# PicoDoorBell — Architecture

How the firmware works and why it is built this way.

Scope: this document describes **what exists**. `ROADMAP.md` describes what is
planned. The `README.md` is the build guide — hardware, wiring and Telegram
setup — and should stay free of internals.

---

## Measured baselines

From the bench unit at v1.23.0, after boot with WiFi connected:

| Measurement | Value |
| --- | --- |
| `gc.mem_free()` | **159,968 bytes** on the current build |
| Filesystem | 4096-byte blocks, 212 total, 202 free (~830 KB free) |
| Ring signal (terminal 04) | Idle at ground, 5 V DC for ~2 s, clean square wave |
| Telegram round trip | 1–2 s, detection to delivery |

The memory figure is the reference for the leak watch: `do_request` closes every
socket in a `finally` and collects afterwards, and the proof that this is
sufficient is free memory staying flat over days rather than any single reading.

> **Compare like with like.** An earlier build read 178,480. The drop is not a
> leak: `main.py` grew from roughly 1,000 lines to 1,720, and MicroPython holds
> compiled bytecode in RAM. A baseline is only meaningful against the same build,
> which is also the first real datum for costing the E2 module split.

---

## Target platform

| | Production | Bench |
| --- | --- | --- |
| Board | Pico W | Pico W |
| MicroPython | v1.23.0 (2024-06-02) | v1.23.0 |
| Power | Direct USB, no battery | Battery module on VSYS, jumper-switchable to direct USB |
| Reset | Push button on the carrier board **and** an external button on wires via connector | Push button soldered to the board only |
| Doorbell input | GP16 | **GP18** |
| Chat target | Telegram channel | Separate bot and channel |

### The bench unit cannot model the reboot fault

Production reboots intermittently and the cause is unresolved. The two remaining
candidates are supply sag and RUN-pin pickup, and **the bench unit is immune to
both by construction**:

- *Supply sag* — the battery on VSYS is exactly the dip-absorbing buffer that
  production lacks. The bench is immune to this one.
- *RUN pickup* — the bench is **less exposed, not immune**. Both boards have a
  button soldered to the carrier, so both have a RUN net. Production's fans out
  further, to a second button on wires through a connector.

A bench unit running cleanly for weeks therefore says little about the reboots.
**The bench validates firmware; reboot diagnosis happens on production.** This is
the main argument for promoting C4 to the production unit rather than waiting for
a long clean bench run.

### Consequences for the RUN investigation

Production's RUN net reaches two buttons and a connector. Disconnecting the
external cable leaves the on-board button and its trace attached, so that
elimination test is **partial**, not complete.

Two buttons on one net also doubles the exposure to a degrading tactile switch. A
contaminated switch can close spontaneously with no mechanical provocation, which
knock-testing would not reveal.

The bisection, once C4 reports a cause:

| C4 says | Conclusion | Next step |
| --- | --- | --- |
| `chip=RUN` | Fault is on the RUN net | External cable already out; next lift the on-board button |
| `chip=POR/BOD` | RUN net exonerated | Supply: swap the USB adapter, check the circuit load |

It also means G6 (RUN noise immunity) cannot be validated on the bench: the
capacitor can be fitted, but there is no fault there to suppress.

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
| **1** | Watchdog scratch registers | soft reset and watchdog only — **any** hardware reset clears them, RUN included | free | Magic word, unstable-boot count |
| **2** | Flash — `state.json` | everything | one 4 KB sector erase | `chatId`, `epochAnchor`, `writes` |
| **3** | Flash — `state.json` | everything | one 4 KB sector erase | Undelivered rings, written only after an outage passes five minutes |

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
2. **Does it only need to survive a soft reboot or watchdog bite?** → **Tier 1**.
   Free, zero wear, 32 bytes total. Note that *any* hardware reset clears these,
   RUN included — confirmed on hardware — so they do not survive as much as the
   name suggests.
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
  "v": 2,
  "chatId": null,
  "epochAnchor": null,
  "writes": 0,
  "boots": 0
}
```

| Field | Purpose |
| --- | --- |
| `v` | Schema version. Drives migration and downgrade safety. |
| `chatId` | Overrides the configured chat after a supergroup migration. |
| `epochAnchor` | Wall-clock epoch captured at the last NTP sync. |
| `writes` | Monotonic write counter. Makes wear observable rather than theoretical. |
| `boots` | Count of **stable** boots. See below. |

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

> **Filesystem confirmed.** `os.statvfs('/')` on the bench unit at v1.23.0 returns
> 4096-byte blocks, 212 total, 202 free — a block size matching the flash sector,
> characteristic of littlefs2, which the rp2 port has defaulted to for many
> releases. The atomic-rename guarantee holds.
>
> Strong inference rather than proof. The decisive test, if ever needed, is case
> sensitivity: littlefs distinguishes `AAA` from `aaa`, FAT does not.

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

## Board definitions

Pin assignments live in `board.py` on the device, copied from one of the
per-revision files in `boards/`:

| File | Board |
| --- | --- |
| `boards/board_v1_2.py` | Production carrier, revision V1.2 — doorbell input on GP16 |
| `boards/board_prototype.py` | Bench prototype — doorbell input on GP18 |

`main.py` holds **no pin defaults**. It imports `board`, checks every name in
`REQUIRED_BOARD_PINS` is present, and raises otherwise.

### Why it refuses to start

A board whose pins are undefined cannot watch the doorbell. Booting into
something that looks alive but is not is precisely the silent failure this
firmware exists to eliminate, so an incomplete install stops at import with a
message naming what is missing.

This costs the LED as a signal — the failure happens before `setup_hardware()`
runs, so the only output is on serial. That is the right trade: the situation only
arises during installation, when a console is attached anyway.

### Why there are no defaults in `main.py`

A default plus an override would leave a pin number in `main.py` that is
authoritative on one board and dead on another. One source of truth is the point;
a fallback would reintroduce the ambiguity the file exists to remove.

### Adding a revision

Add `boards/board_<revision>.py` and copy it to the device as `board.py`. Keeping
superseded revisions in the repo documents the hardware history and keeps older
boards runnable on current firmware. Name files by **revision**, not by role —
"prototype" and "production" describe jobs that move between boards, while a
revision number does not.

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

## Tier 1 — reset diagnosis

The production unit reboots intermittently, correlated with switching mains
loads on the same circuit. The cause is unresolved. Every restart used to look
identical — a startup message in Telegram — which conflates a supply brownout, a
spurious hard reset, and a firmware crash.

Three signals are read at boot. Bench testing then established that only two of
them are worth believing.

| Signal | Verdict it supports | Status |
| --- | --- | --- |
| `CHIP_RESET` register | `POR/BOD` = supply, `RUN` = pin pulled low | ✅ Confirmed on hardware |
| Scratch magic word | Present = no hardware reset occurred | ✅ Confirmed on hardware |
| `machine.reset_cause()` | — | ❌ **Unreliable on rp2. Advisory only.** |
| `WATCHDOG_REASON` | — | ❌ Polluted by the bootrom. Advisory only. |

### `reset_cause()` cannot be trusted here

The RP2040 bootrom uses the watchdog to launch the application, so
`WATCHDOG_REASON` carries the TIMER bit through an ordinary startup and
`machine.reset_cause()` reports it as a watchdog reset.

Observed on the bench unit, same board, minutes apart:

| Actual event | `reset_cause()` said | `CHIP_RESET` said |
| --- | --- | --- |
| Soft reboot after a power-up | `WDT_RESET` | `0x00000100` — POR ✅ |
| RUN pin pressed | `PWRON_RESET` | `0x00010000` — RUN ✅ |

Wrong both times, in different directions. Both values are still logged, in
brackets, because they cost nothing and occasionally corroborate — but nothing
branches on them. Reading three independent signals is what made this visible;
a single-source implementation would have reported confident nonsense.

### Verified register behaviour

- **`POR/BOD` is bit 8, `RUN` is bit 16.** Confirmed against real resets.
- **The flags do not accumulate.** A RUN reset following a power-up reads
  `0x00010000`, not `0x00010100`. Each reset reports only its own cause, so
  `CHIP_RESET` never needs clearing.
- **Any hardware reset clears the scratch area, RUN included** — not only power
  loss, as first assumed.

That last point yields the decision rule:

| Scratch | Meaning |
| --- | --- |
| Cleared (`cold`) | A real hardware reset happened — trust `CHIP_RESET` |
| Intact (`warm`) | No hardware reset: soft reboot, or a watchdog bite once B2 lands. **`CHIP_RESET` is stale**, and is labelled `chip(stale)=` in the log |

### Confirmed boot matrix

Every path verified on the bench unit:

| Event | `raw` | Flags | Scratch | Verdict | `reset_cause()` said |
| --- | --- | --- | --- | --- | --- |
| Power cycle | `0x00000100` | `POR/BOD` | cleared, `boot #1` | `power` | `PWRON_RESET` ✅ |
| RUN pin pressed | `0x00010000` | `RUN` | cleared, `boot #1` | `run-pin` | `PWRON_RESET` ❌ |
| Soft reboot (first run) | `0x00000100` stale | stale | cleared, `boot #1` | `power` ⚠️ | `WDT_RESET` ❌ |
| Soft reboot (counter live) | `0x00000100` stale | stale | survives, `boot #2` | `warm-reset` | `PWRON_RESET` ❌ |

`reset_cause()` was wrong in three of four events, in two different directions.

> ⚠️ The third row is the one ambiguous case: on the very first run of new
> firmware the magic word has never been written, so a soft reboot is
> indistinguishable from a power-on and reads as `power`. It resolves itself from
> the next boot onward, and only ever affects the first boot after a flash.

> Because any hardware reset clears the scratch area, a boot counter kept there
> reported `boot #1` after every power cycle and every RUN reset — useless for a
> reboot investigation that is entirely about power and RUN events. The running
> total therefore lives in flash; see *Counting boots* below.

### Telling a watchdog bite from a soft reboot

`WATCHDOG_REASON` bit 0 is TIMER, an actual timeout; bit 1 is FORCE, which
`machine.reset()` uses. Observed on the bench: `wdt=0x1` when the watchdog fired
during a slow request, `wdt=0x2` for a deliberate reset.

Only trusted on a **warm** boot. The bootrom sets TIMER during an ordinary cold
start, which is what made `reset_cause()` unreliable in the first place.

### Nothing is dropped because a send failed

Three things now survive a failed transmission, on the same rule: **discard only
on confirmation.**

| What | Held in | Retried by |
| --- | --- | --- |
| Rings | `queue` | `flush_queue()` |
| Startup and reconnect notices | `pendingAnnouncement` | `flush_announcement()` |
| The log | `logLines` | `print_log()` |

The announcement matters more than it looks: it carries the reset diagnosis, so
losing it to a momentary outage would quietly cost the reboot investigation its
data. It was previously sent once and forgotten — a regression that surfaced when
the backlog fetch below started arming the backoff before it.

### The update backlog is discarded at boot

`updateId` lives in RAM, so every reset restarts it at zero and `getUpdates`
replays whatever Telegram has been holding — re-executing commands sent up to 24
hours earlier. Observed: a `/log` answered again after every reboot.

`discard_update_backlog()` fetches with `offset=-1` and acknowledges the last
update, clearing everything before it. It runs before the startup announcement,
so a reset cannot replay yesterday's commands.

### Counting boots

Two counters, in different tiers, answering different questions.

| Counter | Tier | Question |
| --- | --- | --- |
| `boots` in `state.json` | 2 | How many times has this device come up and stayed up? |
| Unstable count, scratch 3 | 1 | How many attempts since the last one that stuck? |

The total has to be in flash because Tier 1 does not survive the resets under
investigation. The write is safe because it happens **once per boot, and only
after the device has been up for 60 seconds**.

That gate is doing real work. A boot loop resetting every five seconds would be
17,000 writes a day and a dead sector inside a week — the "never write on a
timer" rule broken by accident rather than design. A looping device never reaches
60 seconds, so it never writes at all.

The consequence is that `boots` counts *stable* boots, not attempts. That is the
more useful number, and the attempts are not lost: the Tier 1 counter carries
them, costs nothing, and is cleared when a boot proves stable. A summary reading
`boot #47 unstable=5` says the device has come up cleanly 47 times and has failed
5 times since the last of them.

**The number is provisional until the gate fires.** A boot that never reaches 60
seconds is never recorded, so the next attempt claims the same number again, with
`unstable` climbing. Observed on the bench: a power-on boot soft-rebooted early
left `boots` at 2, and both attempts reported `boot #3`, the second with
`unstable=2`.

### Verdicts

| Verdict | Condition | Meaning |
| --- | --- | --- |
| `power` | cold + `POR/BOD` | Supply dropped, or first power-up |
| `run-pin` | cold + `RUN` | RUN pin pulled low |
| `watchdog` | warm + `WATCHDOG_REASON` bit 0 | A real timeout |
| `warm-reset` | warm, no TIMER bit | Soft reboot |

> **Reading `warm-reset` on production.** Nothing there interacts with the REPL,
> so spontaneous soft reboots do not occur — once B2 lands, a `warm-reset` on the
> production unit means the watchdog bit. One exception to design around: B4 plans
> a deliberate `machine.reset()` after repeated WiFi failures, which would look
> identical. Intentional resets should set a marker in a spare scratch register
> first, so a self-inflicted reset is never mistaken for a watchdog bite.
>
> Bench boot counts are not comparable: every Ctrl-C and re-run during development
> increments the counter and logs a `warm-reset`.
| `unknown` | cold, no flags | No evidence |

### Register map

| Address | Register | Use |
| --- | --- | --- |
| `0x40058008` | `WATCHDOG.REASON` | Advisory only |
| `0x40058014` | `WATCHDOG.SCRATCH2` | Magic word `0x50444231` |
| `0x40058018` | `WATCHDOG.SCRATCH3` | Boot counter |
| `0x40064008` | `VREG_AND_CHIP_RESET.CHIP_RESET` | Hardware reset record |

Scratch registers 4–7 carry the bootrom's reboot-to-BOOTSEL handshake, so only
0–3 are safe. 2 and 3 are used, leaving 0 and 1 alone in case the port wants
them.

### Why this had to land before the watchdog

Once B2 exists, a wedged network stack produces a reset that would be
indistinguishable from the existing mystery unless the cause is recorded first.

### Failure behaviour

`read_reset_info()` never raises. Every read is individually guarded and the
function returns a fully-populated dict regardless. Diagnostics must not be able
to stop the device booting; a board that will not start is worse than one that
cannot explain why it restarted.

---

## Doorbell input

The input is latched by interrupt. The main loop never reads the pin to decide
whether a ring happened; it only drains what the interrupt already recorded.

### Why polling failed

The old loop read the pin once a second, but the same loop made blocking TLS
calls of one to two seconds and slept five seconds after every press. A ring
landing in one of those windows was lost, and lost **silently** — the physical
chime still sounds, so nobody can report a notification that never arrived.

### Both edges, on purpose

The handler watches rising *and* falling edges, so the pulse width is recorded in
hardware.

The obvious alternative — latch the rising edge, then re-read the pin a moment
later to confirm it is still high — fails in exactly the case that matters. If
the loop was blocked in a handshake for four seconds, the 2 s pulse is long over
and the pin is back at ground. A real ring would be discarded as noise. Capturing
the falling edge means the width is known however late the loop gets there.

### Closing a pulse is provisional

A falling edge records the close, but does not settle it. If the line comes back
up within `DEBOUNCE_MS`, the close is cancelled and the **original rise time is
kept**. `process_input()` likewise refuses to judge a pulse until the line has
stayed low for a debounce window.

This is the part that took two attempts and two hardware runs to get right.

**First attempt:** one debounce window covering both edges. A tap shorter than
`DEBOUNCE_MS` had its falling edge swallowed, so the input stayed latched with no
width and sat there until the 15 s stuck timeout. On the bench, a very short tap
produced complete silence.

**Second attempt:** accept every falling edge immediately. That broke the case
that matters far more. Closing a contact chatters — high, low, high, low over a
few milliseconds before settling — so the *first* fall is part of the make, not
the release. The pulse closed 2 ms in, was judged a transient on those 2 ms, and
the genuine four-second press that followed was discarded. Observed on the bench
as `transient ignored, 2ms` for a long, deliberate press.

The mistaken assumption was that a falling edge means the release. With a
bouncing contact it usually does not.

Provisional closing handles both: chatter on the way in reopens the pulse and
preserves its true start, while a real short tap simply settles and is reported
as the transient it is.

### Interrupt discipline

The handler stamps two integers and returns. No allocation, no I/O, no logging —
MicroPython forbids allocation in an ISR. Input records are plain lists built once
at setup, because assigning to an existing list slot allocates nothing.

### Timings

| Constant | Value | Job |
| --- | --- | --- |
| `DEBOUNCE_MS` | 50 | Ignore contact and optocoupler noise |
| `MIN_PULSE_MS` | 150 | Below this it is a transient, not a visitor |
| `ALERT_LOCKOUT_MS` | 5000 | One alert per ring; must exceed the 2 s pulse |
| `STUCK_INPUT_MS` | 15000 | Held high this long is a fault, not a caller |

The old code used a single five-second `sleep` for all four jobs. They are
separate concerns with different right answers, and the sleep was also what
blocked the loop.

`MIN_PULSE_MS` earns its place beyond noise rejection: the reboot investigation
has not ruled out EMI coupling into this installation, and a bare edge trigger
would turn an injected transient into a phantom notification.

> **Do not tune this on bench data.** Widths measured by hand on a bench supply
> (114–357 ms for taps, 1001–8176 ms for presses) describe a wire being touched,
> not terminal 04. The open question is whether the TwinBus latches its own ~2 s
> signal regardless of how briefly the visitor presses, or tracks the button. If
> it tracks the button, a quick jab could produce a 300 ms pulse and a threshold
> raised to 400 ms would silently reject a real ring — the exact failure this
> project exists to prevent, reintroduced by tuning against the wrong signal.
>
> Settle it by jabbing the real doorbell as briefly as possible and reading the
> logged width. Until then 150 ms stands: above any plausible transient, below
> any plausible ring, and erring toward delivering.

### The log is a list of lines, not a growing string

`log += entry` reallocates the whole buffer on every append. A few hundred
appends during an outage means a few hundred multi-kilobyte allocations and
frees on a 264 KB heap, and TLS handshakes need large contiguous blocks — so the
symptom is **sends beginning to fail while everything small still works**.

That is what a ten-minute bench outage produced: hundreds of appends, then every
send failing, correlated with the log filling. `logLines` is bounded by count and
appends a short string each time; nothing large is reallocated.

> This is the leading explanation for that failure, not a proven one. The
> alternative — a cyw43 stack wedged by repeated `connect()` calls — is addressed
> separately under *Reconnection*. Both fixes are worth having; the next
> occurrence will show which mattered, because the errno is now printed.

It also drops the oldest line once full, where it used to refuse new entries and
print a notice on every call. That preserved the *least* recent events and buried
the console, which is backwards for diagnosis and cost a session.

### `/log` is chunked

Telegram caps a message at 4096 characters. The log was allowed to reach 10000,
so a full log was an unconditional 400 — and the old code then cleared it anyway,
destroying exactly what had just failed to send.

`print_log()` now splits on line boundaries under the limit, sends each piece,
and clears only once every piece has landed. Observed on the bench: `/log` worked
early on and stopped once the log filled.

### Saying things out loud

`report()` prints and logs. Several events that only matter while someone is
watching — ring widths, rejected transients, the stability write — were written
to the in-memory log alone and were invisible on the console. `append_to_log()`
remains for things nobody needs to watch happen.

### Counting what polling would have lost

A missed ring is unobservable, so the firmware counts the cases instead. When a
pulse falls entirely between two passes of the main loop, no poll could have seen
it, and `IN_MISSED` increments. Reported in the heartbeat, this replaces an
estimate with a measurement.

### One input, structured for two

`inputs` is a list of records and G9 would add an entry, not a rewrite — but only
the doorbell is wired today.

---

## Heartbeat

The device reports in every six hours, and on demand via `/status`.

### Why

Its only sign of life used to be `Checking for new messages...` printed once a
minute — a line that says nothing, that nobody watches, and that made silence and
death indistinguishable. It is now log-only, so a long run's console shows events
rather than a metronome.

Free memory is the figure that matters most, because the socket-leak fix in
`do_request` can be proven no other way: a single reading says nothing, only a
flat trend over days does. Reading it by hand meant killing the run to reach a
REPL, which the watchdog then reset out from under you. The heartbeat removes
that trade entirely — a soak now measures itself.

### Contents

Uptime, boot number and reset verdict, free memory, RSSI and IP, per-input
counters (rings, transients, unpollable), queue depth and drops, and the flash
write count. Consecutive network failures and an undelivered announcement are
included only when non-zero, so an ordinary report stays short.

### Uptime is accumulated, not derived

`ticks_ms` wraps at about 12.4 days and is ambiguous past half of that, so uptime
is summed from per-pass differences instead. That is wrap-safe for as long as the
device runs.

### Delivery

Routed through `pendingAnnouncement`, so a failed heartbeat is retried rather
than dropped — a missing "still alive" message is exactly what a dead device
looks like. If that slot already holds a startup report, the heartbeat yields:
the boot diagnosis matters more, and the next one is only hours away.

---

## Undelivered rings

A ring detected during a network outage used to be logged and then lost. Observed
on the bench: `Doorbell ring, 202ms`, then `WiFi is disconnected.`, and no message
ever arrived — detected, measured, counted, and gone, with no crash and no error.

Rings now wait in a queue until Telegram confirms delivery.

### Nothing leaves the queue unconfirmed

`process_input()` used to send and forget. It now enqueues, and `flush_queue()`
removes an entry only on `REQUEST_OK`. A retryable failure leaves it in place; a
`REQUEST_FATAL` drops it, because retrying will not help and one undeliverable
ring must not block every ring behind it.

### RAM first

The queue exists to survive a *network* outage, which RAM covers completely.
Flash only helps across a reset, so it is written **only once an outage has
already run for five minutes** — long enough that a reset in the middle is a real
possibility. A brief outage never writes at all.

| Constant | Value | Purpose |
| --- | --- | --- |
| `QUEUE_MAX` | 20 | A long outage must not exhaust RAM |
| `QUEUE_SNAPSHOT_AFTER_MS` | 300000 | Only persist an outage this old |
| `QUEUE_SNAPSHOT_MIN_MS` | 300000 | And never more often than this |
| `QUEUE_FLUSH_PER_PASS` | 3 | Bound the work in one loop pass |

`state_set()` compares before writing, so a snapshot whose contents have not
changed costs nothing.

### Overflow drops the oldest

At `QUEUE_MAX` the oldest entry goes. A visitor from an hour ago matters less than
the one at the door now, and the count is reported so the loss is visible.

### A restored ring cannot be aged

`ticks_ms` restarts at zero on reset, so a ring recovered from flash has no
knowable age. It is delivered marked `(queued before a restart)` rather than with
a fabricated time. A ring delayed *without* a reset does carry its real delay:
`(delayed 90s)`.

The `Q_EPOCH` field is already in the record and already persisted, holding
`None` until C1 syncs a wall clock. Once it does, restored rings can carry a real
timestamp with no schema change.

---

## Reconnection

One `connect()` is issued, then given 30 seconds to work before another is tried.
After 20 attempts — roughly ten minutes — the board resets itself.

### WiFi power save is off

The CYW43439 defaults to sleeping between beacons, which adds latency and drops
packets — the standard explanation for the timeouts an always-on device sees.
`wlan.config(pm=...)` disables it, applied before connecting since the setting
affects the association.

Both units are mains-powered, so there is no reason to keep it. `board.py` may
set `wifiPowerSave = True` to opt back in; it is read with a default rather than
required, so existing installs keep working. The future battery build is the one
case that wants it on, where the tradeoff inverts and a few dropped packets cost
less than the current draw.

Failure to set the mode is logged, not fatal. An unconfigurable radio still
answers the door.

### Never interrupt a join in progress

`wlan.status()` distinguishes a stalled attempt from one that is working:

| Code | Meaning | Re-issue? |
| --- | --- | --- |
| `1`, `2` | Joining, or associated and awaiting DHCP | **No** — wait up to 20 s |
| `0`, `-1`, `-2`, `-3` | Idle, link failed, no AP, auth rejected | Yes, after 10 s |
| `3` | Connected | Done |

Observed on the bench: **seven attempts over three and a half minutes on a
network that was perfectly available.** Every re-issue aborted a DHCP exchange
that was already under way and started it again. A slower version of the same
mistake as the three-second loop below.

The two intervals must stay ordered: a **dead** attempt should retry sooner than
a **live** one. Setting the allowance to 20 s while stalled retries waited 30 s
inverted that, and a test caught it.

> **Twenty seconds, not sixty.** The first attempt at this used a 60 s
> allowance. On a bench network where DHCP was stalling, that meant a full
> minute of dead time per attempt for no benefit: association plus DHCP
> completes in a few seconds when it is going to complete at all.

MicroPython exposes no `STAT_` constant for 2, and names `-3`
`STAT_WRONG_PASSWORD` even though cyw43 reports it for any association
rejection — MAC filtering or an AP block included. Both were misleading in the
logs, so the firmware carries its own names.

### Why not just retry

The previous loop called `wlan.connect()` every three seconds for as long as the
network was down. A ten-minute bench outage issued around 200 of them, and
afterwards the board **associated but passed no traffic**: `wlan.status()`
returned 3, the IP printed, and every send failed. Re-issuing a connect while one
is already in flight is a known way to wedge the cyw43 stack.

A wedged stack is also why the attempt count ends in a reset rather than more
retrying. Resetting cannot fix an absent router — but by that point an absent
router is no longer the likeliest explanation, and a reset is the only thing that
clears a wedged driver.

### Associated but dead

`wlan.status()` reporting a connection is not evidence that traffic flows.
Reproduced on the bench: after the AP rejected the board for a few minutes, it
re-associated — status 3, IP printed — and every DNS lookup then returned `-2`,
indefinitely. The same signature had appeared once before after a ten-minute
outage.

B4's attempt counter does not help. It only guards the connection phase, and
once status reads 3 the reset path is out of reach. **A stack that claims to be
up and passes nothing is worse than one that admits it is down, because nothing
notices.**

`note_request_result()` counts consecutive transport failures. A `REQUEST_FATAL`
does not count — Telegram answered, so the link is fine. Failures also back off,
doubling from 5 s to a 60 s cap; without it a long outage burns hundreds of DNS
lookups an hour and floods the log.

The backoff is shared across every request, since they all go to the same host.
A queued ring failing on each loop pass therefore keeps re-arming it, and the
once-a-minute `getUpdates` is usually skipped before it opens a socket. The
failure line states the delay, so a quiet console is not mistaken for a quiet
network.

A skipped request returns `REQUEST_SKIPPED`, not `REQUEST_RETRY`. It was
previously indistinguishable from a transport failure, which produced log entries
like `getUpdates failed: status=0` for requests that never happened, and let a
single outage count twice toward the dead-network deadline. **A request that was
never attempted is evidence of nothing.**

### The remedy escalates

| Time since the last successful request, while associated | Action |
| --- | --- |
| `NETWORK_DEAD_MS` (2 min) | **Bounce the link** — `disconnect()`, then reconnect |
| Another `NETWORK_DEAD_MS` after that | **Reset** |

> **Five minutes, and deliberately generous.** An associated-but-dead link
> recovered on its own after about seven minutes on the bench, with no
> intervention — a wedged driver does not do that, so the cause was upstream.
> Bouncing at two minutes would have discarded a working association several
> minutes before the network returned, and reassociating on that router took 28
> attempts. Nothing is lost by waiting, because the queue holds the ring, while
> acting early can make recovery slower.

> **Elapsed time, not a failure count.** The first version counted fifteen
> consecutive failures. Backoff stretches a count into an unpredictable
> duration — fifteen worked out at roughly twelve minutes, and on the bench the
> AP deauthenticated the board long before that, which made
> `is_wifi_connected()` false and left the whole detection path unreachable. A
> deadline says what was meant.

The bounce comes first because the firmware **cannot tell a wedged local stack
from an upstream block**. A router that filters a device while leaving
association and DHCP intact produces an identical symptom — and that is exactly
what happened during testing, which is why the wedged-stack reading here is
uncertain. Resetting cannot fix an upstream block; repeating it every few minutes
through an ISP outage would be pure churn, and each reset destroys the RAM log.

A bounce keeps the log, the queued rings and the uptime. Only if it fails to help
is a reset the remaining local action.

The bounce is *requested* from `note_request_result()` and performed by the main
loop, because that function runs deep inside a request and reconnecting from
there would be re-entrant.

> **Three explanations, and two falsified.** Heap fragmentation from the growing
> log string was first: it predicted the failure would stop once the log became a
> list, and it did not. A wedged cyw43 stack was second: it predicted the state
> would persist until something cleared it, and instead the link recovered on its
> own after roughly seven minutes. What remains is an upstream block — the router
> holding a device restriction for some time after it is lifted.
>
> The escalation stays, because a genuinely wedged stack is still possible and the
> bounce is cheap. But the thresholds are set on the assumption that patience is
> usually the right answer.

### The reset reason outlives the reset

The RAM log does not survive a reset, which is worst exactly when resets repeat:
an AP that keeps rejecting the board produces a reset every few minutes, and each
one destroys the log explaining why.

`self_reset()` therefore writes a short code to scratch 0, and the next boot
reports it — `reason=WiFi_unreachable` or `reason=network_dead_after_a_bounce` in
the summary. One fact, carried across, at no cost in flash.

### Self-inflicted resets are marked

`self_reset()` writes a marker to scratch 1 before resetting, so the next boot
reports `verdict=self-reset` rather than `warm-reset`. Without it, the firmware's
own reset would be indistinguishable from a watchdog bite — both are warm with no
`CHIP_RESET` flags — and would pollute the reboot dataset.

It also snapshots the queue first, so undelivered rings survive.

> **Unverified:** scratch 0 and 1 are believed unused by the port. Only 4–7 are
> documented as taken by the bootrom.

### The queue is persisted during an outage

`connect_wifi()` blocks the main loop for the whole outage, so
`maybe_snapshot_queue()` is called from inside its wait. Otherwise the queue
would never reach flash during the one situation it exists for.

---

## Watchdog

`machine.WDT` at 8000 ms, armed during boot. **An RP2040 watchdog cannot be
disarmed once started.**

### The margin is thin, and bought deliberately

The chip's ceiling is roughly 8.3 s. Measured worst case for one loop pass:

| Step | Worst case |
| --- | --- |
| `send_message` after a ring | 2 s |
| `read_message` on the 60 s tick | 2 s |
| Flash sector erase at the stability gate | ~0.3 s |
| `loopDelay` | 1 s |

That fits, but not by enough to trust on a bad day. So the watchdog is fed from
**inside** the blocking work — before and after every request, after the flash
write, and in slices during every wait — rather than only at the top of the loop.

**B1 was a prerequisite.** With its 5 s post-press sleep still in place, a ring
followed by a `getUpdates` could pass nine seconds with no feed, and the watchdog
would have reset the board during ordinary operation.

### `sleep_fed()`

Any wait longer than the timeout resets the board. The 10 s grace period after an
error was exactly that: left as a single `time.sleep(10)`, arming the watchdog
would have turned every transient fault into a reboot. `sleep_fed()` breaks waits
into 500 ms slices and feeds between them.

A test walks the AST and fails if any raw `time.sleep()` with a constant argument
of 8 s or more survives. The short ones that remain — LED blinks, and
`error_halt()` — are either brief or run before the watchdog is armed.

### When it is armed

After hardware and state are up, before networking. Two reasons:

- A wedged cyw43 stack is the failure this chiefly exists for, so the network
  phase must be covered.
- Not earlier, because `error_halt()` blinks forever by design. A watchdog there
  would convert a visible configuration fault into a silent reset loop.

`connect_wifi()` feeds while retrying, so a genuinely unreachable router does not
cause a reset — resetting would not fix it. The watchdog is for a wedged stack,
not an absent network.

### If the watchdog cannot be created

Logged, and boot continues. An unguarded device still answers the door; one that
refuses to start does not.

### Consequence on the bench

Ctrl-C leaves the main loop, so nothing feeds, and the board resets a few seconds
later. That is correct in service — an exited loop is a dead doorbell — but it
means development interruptions now produce a reset and a `warm-reset` verdict.
The firmware says so on the way out.

---

## HTTP request layer

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
| `REQUEST_SKIPPED` | Never attempted — backoff, or the link is down | wait; this is evidence of nothing |

### The timeout cannot fully prevent a watchdog reset

`timeout` applies **per socket operation**, not per request. DNS, connect, the
TLS handshake and the read each get their own budget, so a request can outlast
the 8 s watchdog even with a timeout set — observed on the bench as a reset
landing immediately after `HTTP GET failed: ETIMEDOUT`.

There is no headroom on the other side: the RP2040 caps the watchdog near 8.3 s,
and feeding from a timer during a request would defeat the point of having one.
`getaddrinfo` can also block outside the timeout entirely.

The timeout stays at **5 s**. Three was tried after a run of `ETIMEDOUT`
failures, but those were most likely the inter-VLAN hop and WiFi power save
rather than anything a timeout could fix — both since removed. Tuning against a
problem that is being eliminated leaves a margin tighter than the hardware needs
and turns healthy-but-slow requests into retries.

**This reduces the exposure; it does not remove it.**

The response is to make a reset cheap rather than to prevent it: the boot counter
is in flash, the reset reason is in a scratch register, and a ring queued while
the network is degraded is written to flash immediately. What a reset still costs
is the RAM log.

### A dropped network must not become a reset

Two guards, because the failure was observed twice on hardware: WiFi vanished
while a request was in flight, `urequests` blocked with no timeout, and the
watchdog reset the board — once during a send, losing the ring, and once during
`getUpdates`.

1. **Pre-flight check.** `do_request` returns `REQUEST_RETRY` without opening a
   socket if `is_wifi_connected()` is false. Free, and it covers the common case
   where the network went away before the call.
2. **Request timeout**, at 5 s, below the 8 s watchdog. A stalled socket becomes
   an outcome the caller can retry rather than a reset. Passed only if
   `urequests` accepts the parameter; older builds fall back to an untimed call,
   noted once in the log.

Neither is complete. A network that disappears mid-handshake, or a `getaddrinfo`
that blocks, can still exceed the timeout, and **the watchdog remains the final
backstop**. That is the correct order of defences — but a reset should be the
last resort, not the routine response to a flaky router.

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
| `board.py` | Pin assignments for this board. Copied from `boards/` at install time. |
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

`tests/test_reset.py` covers reset diagnosis: cold versus warm boot, the counter
surviving a warm reset and restarting after a hardware one, stale-flag handling,
unknown cause codes, and the verdicts — using the exact values the bench unit
reported, including the two cases where `reset_cause()` disagreed with the
hardware. 35 assertions.

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