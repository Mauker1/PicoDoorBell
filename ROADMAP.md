# PicoDoorBell: Refactor Roadmap

Baseline for this document is `main.py` and `secrets.py` as published on `main`.

Target: the bot must work identically whether it notifies a **DM**, a **group**, a
**supergroup**, or a **channel**.

---

## Guiding principle

The current firmware is a **silent-failure appliance**. When it breaks (WiFi wedged,
socket leak, unhandled `KeyError`, chat migrated) nothing tells you. You find out when
someone says *"I rang and you never came."*

Every P0 item below exists to convert a silent failure into either a self-healing
recovery or a visible alert. Everything else is correctness and polish.

## Priority scale

| Tag | Meaning |
| --- | --- |
| **P0** | Device silently fails or loses events. Fix first. |
| **P1** | Device behaves incorrectly, but observably. |
| **P2** | Quality, diagnosability, maintainability. |
| **P3** | Polish, cleanup, cosmetic. |
| **P4** | Roadmap / future hardware. |

## Status

| Mark | Meaning |
| --- | --- |
| ✅ | Done: running on production, or, for documentation and investigations, finished |
| 🔬 | Bench-verified: tested and passing, waiting to be promoted |
| 🧪 | On the bench **now**, verification not yet complete |
| - | Not started |

The bench is hardware too, so "verified on hardware" was never the distinction. Two things
separate the marks: whether the production unit is running it, and whether the bench has
actually finished testing it. 🧪 exists because "flashed" and "verified" are days apart for
anything that needs a soak: a memory trend, a wear count, a full log.

Per-item progress lives in [Sequencing](#sequencing). Bench procedure and promotion
steps live in `docs/TESTING.md`. This section is current state only.

### What is running where

| | Firmware | Notes |
| --- | --- | --- |
| **Production** | The C5 build | Still polls the input; no watchdog, no queue. Everything from `B1` onward is bench-only. |
| **Bench** | Current tree | MAC `28:cd:c1:00:10:ed` |

Production MAC is `d8:3a:dd:af:cc:82`, so the two units are not confusable.

### The reboot dataset

`C4` is deployed on production and calibrated: re-plugging the reset connector produced
`verdict=run-pin chip=RUN raw=0x00010000` there, so **if the spontaneous reboots come
through the RUN line, they will say so.** That replaced the earlier elimination approach,
which was weak because no fault could be demonstrated in the known-bad configuration.

**Discard `boot #1`.** It reported `verdict=power` on a soft reboot, which is the documented
first-run ambiguity, since the magic word had never been written. The dataset starts at
`boot #2`.

The re-plug itself was a calibration event, not a data point: connector travel is mechanical
and does not happen in service.

**Production setup and observations, dated, so attribution stays honest.**

- A Brennenstuhl surge protector was added to the production USB PSU a few days *before* the
  firmware upgrade, not at the same time. It therefore predates the current build and is part
  of the fixed setup the dataset is now collected under.
- With the surge protector already in place but still on the *old* firmware, the original
  trigger was actively reproduced: mains loads on the same circuit were switched on and off
  many times, deliberately. Production did not reboot. This is an active reproduction of the
  suspected supply/transient fault under the new power conditioning, and it held, which is
  stronger evidence than passive uptime, though not proof: the original switch correlation
  was itself informal (recall bias, see below), and there is no negative control run without
  the protector.
- After the firmware upgrade, production took one unattended reboot: `boot #6`,
  `verdict=watchdog wdt=0x1` (TIMER) at ~03:57 CEST, with the surge protector already
  installed. A surge protector cannot affect a watchdog bite, which is a loop stall, not a
  supply event, so this reboot is untouched by it. No recurrence in the ~2 days since.
- The router auto-updated at ~03:40 CEST one night. The bench (a different subnet, behind an
  inter-VLAN hop) lost its association and correctly announced a reconnect; production held
  its association throughout, its once-a-minute poll never faltering and no failure lines in
  the log across the window. Different networks, two truthful outcomes.

**Working reading: two distinct faults, with asymmetric evidence.** A supply/transient fault
(switch-correlated, would read `verdict=power`) that the surge protector plausibly addressed,
and a watchdog/stall fault (`verdict=watchdog`, the 03:57 bite) that it cannot touch and that
still lives in the firmware's request/DNS path. What confirms or refutes each over time: any
future `verdict=power` reboot means the supply path is not fully solved; any future
`verdict=watchdog` is the stall fault, unaffected by the power setup, and the one that would
most benefit from `C1` on production so the next bite is legible against the wall clock.

**Bench power reboots are a rig artifact, not part of this dataset.** The bench is fed through
an Anker USB-C hub whose own power comes from the MacBook's PSU; when that PSU is interrupted,
the laptop falls back to battery and the switchover gap browns out the Pico. Reproduced
deliberately: pulling the hub's PSU produced `verdict=power` on demand. A cluster of three
`verdict=power` boots in 16 minutes, unattended, was the same mechanism (the upstream feed
flickering). This is mundane, bench-only, correctly diagnosed by C4 every time, and shares no
mechanism with production's watchdog fault. Production's power path is entirely different
(wall adapter plus surge protector, no hub, no battery fallback). The bench's clustered
`verdict=power` events must not be read as evidence about production; they are noted here only
to sever that false link. The fix, if stabler bench soaks are wanted, is physical (feed the
Pico directly, or keep the hub PSU stable), not firmware.

### Reading a reboot

| Verdict | Meaning | Next step |
| --- | --- | --- |
| `power` | Supply: brownout or dip | Swap the USB adapter (queue item 9) |
| `run-pin` | RUN line pulled low | Bisect: lift the on-board button |
| `watchdog` | A real timeout; the loop stalled past 8 s | Check what preceded it |
| `self-reset` | The firmware chose it; `reason=` says why | Read the reason |
| `warm-reset` | Soft reboot | Only expected on the bench |
| `unstable=N` | N attempts since the last stable boot | A loop; investigate |

`watchdog` and `self-reset` cannot occur on production until the current tree is promoted.

### Known cosmetic issue

The startup message still says "for the first time" above a boot counter (`C6`).

---

Categories group work by *similarity*; priorities sequence it. Work the priorities
across categories, not the categories top to bottom. See
[Sequencing](#sequencing) for the concrete order.

---

## A. Bug fixes

### A1: Universal update parsing (P1, portability)
> **Not a live bug here.** This deployment uses a channel, so the existing `channel_post`
> parser is correct. A1 matters for the DM and group paths the README documents, and for the
> non-text updates any chat type can deliver.

Replace `result['channel_post']` with a resolver walking the known update keys in order:
`message`, `edited_message`, `channel_post`, `edited_channel_post`. Return `None` for
anything else (`my_chat_member`, `callback_query`, service updates) rather than raising.

Extract `chat.id`, `chat.type` and `text` defensively: a post with no `text` key (photo,
sticker, join event) must be skipped, not crash.

This is the change that makes DM / group / supergroup / channel work from one code path.

> **Why it matters:** `channel_post` is emitted *only* for channels. In a group or a DM,
> every update raises `KeyError`, which propagates to the catch-all handler and triggers a
> spurious `wlan.disconnect()`. Even in a channel, a photo or sticker post has no `text` key
> and does the same.

### A2: Command matching that survives group conventions (P1)
Parse as `text.strip().split()[0].split('@')[0]`, compared case-insensitively, so `/log`,
`/log@PicoDoorBellBot` and `/log extra args` all match. In groups Telegram's client
appends the bot username via autocomplete, and does so mandatorily when multiple bots are
present. Add a small dispatch table so future commands don't mean touching the parser.

### ✅ A3: HTTP status checking (P0)
`send_message` and `read_message` must inspect `response.status_code`.

`urequests` **does not raise on 4xx**: it returns a response object with the error status.
Today every API failure is invisible: the code prints "Doorbell pressed!" and carries on
believing it delivered.

Classify:

- `2xx`: success
- `429`: read `retry_after` from the body, back off
- other `4xx`: permanent, log and give up
- `5xx` / `OSError`: transient, retry with backoff

Prerequisite for A4 and A6.

### A4: Chat migration handling (P0)
On a 400 response, check the body for `parameters.migrate_to_chat_id`. If present, persist
the new ID (Tier 2, see [I1](#i1--tiered-state-model--p1)) and retry.

> **Why it matters:** a group becomes a supergroup automatically when made public, when it
> exceeds the member threshold, and in some cases on admin assignment. The chat ID format
> changes from `-123456789` to `-100123456789` and the old ID stops working. Without this,
> notifications stop permanently and silently.

Depends on A3, I2.

### ✅ A5: Guaranteed response cleanup (P0)
Every request in `try/finally` with `response.close()` in the `finally`. Follow each request
with `gc.collect()`.

Currently `response.close()` sits after the `for` loop in `read_message`, outside any
guard: any exception during parsing leaks the socket. This is the classic MicroPython +
`urequests` slow death: works for days, then `ENOMEM`. The existing `MemoryError` recovery
path (disconnect WiFi, sleep 10 s) frees nothing.

### 🧪 A6: Log lifecycle correctness (P1)

> **Still unverified:** the chunking path has never met a log long enough to split. The bench log has not passed 4096 characters since it landed.
Three defects, one fix:

1. Clear the log after a **confirmed successful send**, not only on overflow. Today
   `print_log` resets only when `len(log) > logMaxSize`, so `/log` against a small log
   leaves it untouched, contradicting the README.
2. Chunk output to Telegram's **4096-character** limit. `logMaxSize` is 10000, so a full
   log is an unconditional 400.
3. **Never discard a log that failed to send.** Today the 400 is invisible (A3), control
   returns, the overflow condition is true, and `reset_log()` wipes it: you ask for the
   log, nothing arrives, and the log is destroyed.

Depends on A3.

### 🧪 A7: Boot-time backlog flush (P1)

> **Still unverified:** no backlog has existed to discard since it was flashed, so `Discarded N stale update(s)` has never appeared.
Call `getUpdates` with `offset=-1` once at startup to discard the buffered backlog.
`updateId` starts at 0, so the first poll returns up to 100 buffered updates and a stale
`/log` from hours ago replays on every reboot.

### A8: Query string cleanup (P3)
`?offset=X` then `&`, and drop the `chat_id` parameter entirely - `getUpdates` does not
accept it and Telegram silently ignores it.

> **Note:** this works today. A `?` is legal inside a query component, so the parameter
> parses as key `offset` with value `123?chat_id=456`, and Telegram reads the leading
> digits and stops at the first non-digit. It is not a bug; it is reliance on undocumented
> parser leniency. Cheap to fix, low priority.

---

### 🔬 A9: Request timeouts and a pre-flight connectivity check (P0)
Observed twice on the bench: WiFi dropped while a request was in flight, `urequests` blocked
with no timeout, and the watchdog reset the board. Once during a send, losing the ring, and
once during `getUpdates`.

The earlier three-minute outage test passed only because the outage began while the loop was
idle, so `connect_wifi()` noticed and retried, feeding as it went. An outage that starts
*during* a request takes the other path.

Two guards: skip the request entirely when WiFi is already down, and pass a 5 s timeout when
`urequests` supports it, falling back to an untimed call otherwise. Neither is complete: a
network that vanishes mid-handshake can still exceed the timeout, so the watchdog stays the
final backstop. But a reset should be the last resort, not the routine answer to a flaky
router.

**This gated promotion.** Without it, a flaky network would have produced repeated watchdog
reboots on the unit being used to measure reboots.

✅ **Verified on hardware.** A network outage now produces sixteen retry cycles and a clean
reconnection with no reset, where the same test previously reset the board. The reconnect
message also carries no reset summary, confirming that fix.

Inconclusive: no "urequests has no timeout support" line appeared. Either the build accepts
the parameter, or the pre-flight check caught every case before the timeout path was reached.
The guard that fired is the one that mattered.

---

## B. Reliability

### 🔬 B1: IRQ-driven doorbell input (P0)
**The headline fix.** `Pin.irq(trigger=IRQ_RISING)` sets a latch flag; the main loop only
drains it.

Today the input is polled once per second, but the same loop performs a blocking `getUpdates`
every 60 s (TLS handshake on a Pico W is easily 1-3 s) and sleeps 5 s after a detected press.
During all of that the pin is not observed at all. **A short bell pulse landing in one of
those windows is lost silently.**

Requirements:

- ISR does nothing but set a flag and return: no allocation, no I/O
- Timestamp-based debounce (ignore edges within N ms)
- Must tolerate the CPU stalling during a flash erase (see [Flash](#appendix--flash-wear-analysis))

**Design parameters, now that G7 is measured:**

- **Trigger:** `IRQ_RISING` on a 0 → 5 V transition.
- **Debounce and lockout are separate timers**, which the current 5 s sleep conflates.
  Debounce is tens of milliseconds, to reject edge noise. Lockout must exceed the 2 s pulse
  so one ring yields one notification; the existing 5 s is a sensible value.
- **Validate the pulse width before notifying.** Require the input to stay high for
  100-200 ms before accepting a ring. Against a 2 s signal this costs nothing, and it
  rejects short transients outright.

**Instrument the misses.** Count presses that the IRQ latches *while a blocking operation is
in progress*: those are exactly the rings the old polling loop would have dropped. Report
the count in the heartbeat (C3).

> A missed notification is unobservable by construction: the physical chime still sounds, so
> a dropped alert only matters when nobody is home to hear about it. Neither firmware version
> has ever shown a missed ring, which usefully bounds the rate from above but cannot
> distinguish zero from a handful per year. The counter replaces the estimate with a
> measurement, and costs nothing: the latch already has the timestamp and the loop already
> knows when it was busy.

> **Why validation matters here.** The reboot investigation has not ruled out EMI coupling
> into this installation (see hardware queue item 10). If a transient ever reaches the input,
> a bare edge trigger would send a phantom notification; a validated pulse would not. Cheap
> insurance against a failure mode we cannot currently exclude.

> **Structural note, not a feature.** Build the latch **per pin** rather than as a single
> module-level flag. G9 (distinguishing the building entrance from the flat door) would need
> a second input, and a per-pin structure makes that an extra entry rather than a rewrite.
>
> This is a few lines' difference and adds no complexity now. **B1 still handles exactly one
> input**: G9 is deferred and unproven, and building for a hypothetical second input would
> be over-engineering. The point is only to avoid a structure that would later have to be
> undone.

### 🔬 B2: Watchdog (P0)
`machine.WDT`, fed from the main loop. Timeout must accommodate **both** a slow TLS
handshake (~8 s) **and** a worst-case flash sector erase.

> ✅ **Measured: 1-2 s per Telegram round trip** on the bench unit, detection to delivery.
> Healthy on its own, but the RP2040 watchdog ceiling is roughly **8.3 s** and the current
> loop can exceed that in one iteration:
>
> | Step | Worst case |
> | - | - |
> | `send_message` after a press | 2 s |
> | `time.sleep(buttonDelay)` | 5 s |
> | `read_message` on the 60 s tick | 2 s |
> | **Total with no feed** | **~9 s** |
>
> **B1 is therefore a hard prerequisite, not a preference.** It replaces the blocking 5 s
> sleep with a timestamp lockout, removing the bulk of the exposure. The watchdog should
> also be fed inside `do_request`, so a slow handshake cannot trip it either. Feeding only
> at the top of the loop would reset the device during ordinary operation.

A wedged cyw43 stack currently requires someone to physically power-cycle the unit.

### ✅ B3: Fully guarded boot path (P0)
Configure the GPIO and IRQ **before** touching the network, and wrap the entire startup in
exception handling.

Today `connect_wifi()` runs at module scope outside the `try`, and sends `startupText`
immediately after association: precisely when DNS is least likely to be ready. If that
first send throws, the script dies with a traceback and `doorBellInput` is **never
created**. With no watchdog, the device is dead until someone power-cycles it.

Also: remove the message-sending side effect from inside `connect_wifi()`. Connection and
notification are separate concerns.

### 🔬 B4: Bounded WiFi reconnection with backoff (P1)
> **Mark the reset as intentional.** The `machine.reset()` after N failed cycles would
> otherwise be indistinguishable from a watchdog bite: both read as `warm-reset`. Set a
> marker in a spare scratch register before resetting, and clear it once read, so
> self-inflicted resets never pollute the B2 watchdog data.

Interpret `wlan.status()` properly (`-3` wrong password, `-2` no AP found, `-1` link fail,
`3` connected). Exponential backoff instead of the current fixed 3 s hammer. After N failed
cycles, `machine.reset()`.

Distinguish permanent misconfiguration (bad password, where retrying forever is pointless, so blink
an error code) from a transient outage.

### B5: Proportionate error recovery (P1)
Stop calling `wlan.disconnect()` on every exception. One failed POST currently tears down
the network, which triggers a reconnect and an *"I am back online!"* message, turning a
flaky moment into notification noise.

Classify: network errors → retry; parse errors → log and continue; `MemoryError` →
`gc.collect()`, then reset if it recurs.

### 🔬 B6: Offline event queue (P0)
> **Demonstrated cleanly.** With A9 in place, dropping WiFi just after a press produces:
> `Doorbell ring, 202ms` followed by `WiFi is disconnected.`, with the ring detected, measured,
> counted and logged, then lost. No crash, no error, no trace beyond that line. This is now
> the last real gap in Phase 1.
>
> **Observed on the bench, not hypothetical.** A press produced no notification: the send
> hung, the watchdog bit at 8 s, the board reset, and the ring was gone. `process_input()`
> marks a ring delivered (incrementing the counter and setting the lockout) *before* calling
> `send_message()`, and ignores the outcome. Two consequences B6 must cover:
>
> - **A failed send must re-queue**, not silently drop. The return value is already there
>   and already unused.
> - **A ring latched but not yet sent is lost to a reset.** The RAM queue does not survive
>   the watchdog, and a watchdog bite is not a deliberate reset, so the Tier 3 snapshot as
>   specified would not fire either. Either snapshot on latch when a send is in flight, or
>   accept the window and document it.
>
> Root cause of the hang: `urequests` sets no socket timeout, so a half-open TLS connection
> blocks indefinitely. Nothing can feed the watchdog during a single blocking call. The
> reset is correct behaviour, since before B2 that hang would have wedged the device silently,
> but the ring should survive it.

Latch doorbell events with timestamps into a bounded **RAM** queue and flush on reconnect.
Messages delivered late must be marked as delayed.

Today a press during a WiFi outage is simply lost: the one thing a doorbell notifier must
never do.

**RAM-first by design.** The queue exists to survive a *WiFi* outage, which RAM handles
completely. Flash only helps in the narrow case of a press followed by a reset before
delivery. Snapshot to flash (Tier 3) only on a rare trigger: queue non-empty *and* outage
exceeding a threshold, or immediately before a deliberate `machine.reset()`. This cuts write
count by roughly two orders of magnitude versus persisting every event, at almost no cost in
real-world reliability.

### B7: Alert rate limiting (P2)
Cap alerts per rolling window. A stuck-high input line currently means one message every
5 seconds, forever. On suppression, send **one** "input appears stuck" notice rather than
falling silent.

### 🔬 B8: WiFi power save (P2)
`wlan.config(pm=0xa11140)` for the mains-powered build; the well-known Pico W latency fix.

Make it a **config flag**, not a hardcoded call: the battery build will want the opposite
tradeoff.

---

## C. Observability

### 🔬 C1: Real timestamps (P1)

> Bench-verified over a clean 23 h soak (boot #34): NTP synced at boot and resynced
> unattended twice on the 12 h timer, the wall clock stayed coherent across the whole run
> (per-minute log lines one minute apart for hours), free memory was flat and fully
> recoverable (147,168 bytes to the byte at 6/12/18 h; the only dips were the bounded log
> filling, and `/log` clearing it returned the memory), and flash writes stayed flat (one
> write in 23 h). `tests/test_clock.py` covers the logic in 34 assertions. Not yet on
> production, held deliberately so the reboot investigation keeps a stable build to reason
> against. The `ticks_ms` wrap (~12.4 days) is covered by the anchor model and `ticks_diff`
> in tests rather than by soak, since forcing it needs a two-week run.

NTP sync at boot with periodic resync. Fall back to `ticks_ms` when unsynced and mark those
entries as such.

`ticks_ms` wraps at ~12.4 days, and `3847221` is useless in an incident report.

**Design: an anchor, not repeated polling.** One NTP read captures the epoch at a known
`ticks_ms`; any later wall-clock time is `anchor + elapsed`, with `ticks_diff` keeping the
elapsed term wrap-safe. The derived clock therefore outlives the raw counter that feeds it.
The anchor lives in RAM and is authoritative for "is the clock live"; the copy in
`state.json` (`epochAnchor`) is a coarse fallback for aging rings recovered after a reset,
and restored-ring times are tagged approximate accordingly.

**Fallback marking.** A log line before the first sync carries a `t`-prefixed `ticks_ms`
value (for example `t3847221`), so a relative stamp can never be read as an absolute one. A
line after the sync carries a real timestamp, so a log spanning a sync shows exactly where
real time began.

**UTC, by choice.** NTP only ever yields UTC; converting to local time is always the
client's job, and there is no timezone database on a part this size. A fixed offset cannot
follow daylight saving, so it would be silently an hour wrong for months of the year, worst
in exactly the case the board clock exists for (an offline history of when rings happened).
The device therefore shows and logs UTC. `utcOffset` (seconds, optional, defaults to 0) is
read the same way as `wifiPowerSave` and applied at display time only, so the stored anchor
stays UTC; it remains an escape hatch for a fixed-offset install, and every timestamp is
tagged with the offset it used (for example `+0000`) so any such choice stays visible. A
DST-aware, portable variant is possible but was judged not worth the cost here: it needs
per-zone rules, which is what makes it either large or non-portable. See the note in
`docs/ARCHITECTURE.md`.

**Never fatal.** NTP sync is best effort, exactly like `announce_startup`: a device with no
clock still answers the door, an implausible epoch (below a 2020 sanity floor) is rejected
rather than anchored, and the blocking UDP call is bracketed by watchdog feeds with a low
socket timeout.

### 🔬 C2: Structured, bounded logging (P2, partially done)
> Bounding and the ring behaviour landed early: the log is now a bounded list of lines rather
> than a growing string. Levels and memory instrumentation are still outstanding.

Levels (DEBUG / INFO / WARN / ERROR). Ring buffer that drops oldest rather than refusing new
entries: the current `append_to_log` stops accepting anything once full, so the log
preserves the *least* recent events. Include `gc.mem_free()` and uptime on every entry so a
leak is visible as it develops.

Also remove the debug noise: `print(response.text)` materialises a full `str` copy alongside
the cached `bytes` body on a 264 KB part, and the exception handler dumps the entire ~10 KB
log to console on *every* error.

**Explicitly Tier 0: RAM only.** See [I1](#i1--tiered-state-model--p1). Crash survival is
handled by a compact code in the scratch registers plus, optionally, a *single* flash write
on the way to a reset. Never continuous log persistence.

### 🔬 C3: Heartbeat (P0)

> ✅ **Verified.** Free memory: 159,968 at 2 min, 153,984 at 71 min, 153,184 at 181 min,
> then **153,264 at both 360 and 720 min**: identical to the byte across six hours of
> continuous polling. The early decline was the log filling, and the buffer caps at 120
> lines.
>
> Two other things fell out of the same run: heartbeats arrived unprompted at six and
> twelve hours, so the schedule works unattended, and flash writes stayed at 38 for twelve
> hours, confirming empirically that nothing writes on a timer.
>
> **This closes the socket-leak question** from the original review. It could not have been
> answered without the heartbeat: every attempt to read the figure by hand ended the run
> that was producing it.

Periodic "alive" ping carrying uptime, free memory, RSSI, alert count and flash write count.
Optionally a `/status` command for on-demand health.

This is the single change that converts silent failure into visible failure. Rank it
alongside B1 in value.

Keep the payload schema **extensible** so battery voltage can be added later without a
breaking change.

### ✅ C4: Reset cause and boot counter (Tier 1) (P1)
Read `machine.reset_cause()` at boot, keep a boot counter in a watchdog scratch
register, log both, and include them in the startup message and heartbeat.

> **Must land before B2.** The production unit reboots intermittently, correlated with
> switching mains loads on the same circuit, and the cause is unknown. Today every restart
> looks identical: a startup message in Telegram. Once the watchdog exists, a wedged
> network stack produces a reset indistinguishable from that mystery unless the cause is
> recorded first.

Distinguishes:

| Cause | Meaning |
| --- | --- |
| `PWRON_RESET` | Supply dropped: brownout or mains dip |
| `HARD_RESET` | RUN pin driven low by the button, or transient pickup on the trace |
| `WDT_RESET` | Firmware wedged (once B2 exists) |
| `DEEPSLEEP_RESET` | Future battery build only |

Roughly 20 lines. Converts an ongoing mystery into a dataset, and its value grows with
uptime, so it wants to reach the production device early.

### ✅ C5: Persist the boot counter to Tier 2 (P1)
The Tier 1 boot counter counts almost nothing useful. Bench testing showed any hardware
reset clears the scratch area: a power cycle and a RUN press both report `boot #1`. It
therefore only counts soft reboots and watchdog bites, and contributes nothing to the
production reboot dataset, which is entirely about power and RUN events.

C4's *cause* half works and is the more important half. The *count* half does not.

**Fix:** move the counter to Tier 2 (`state.json`), keeping the magic word in Tier 1 for
warm/cold detection. One write per boot is affordable: daily reboots are ~365 writes a year
against ~100,000 cycles.

⚠️ **Boot loops are the hazard.** Resetting every five seconds would be 17,000 writes a day
and a dead sector within a week: the "never write on a timer" rule violated by accident.

> ✅ **Validated on hardware.** A power-on boot that was soft-rebooted before 60 s was not
> recorded: `boots` stayed at 2, the next attempt reported `boot #3` again, and
> `unstable=2` counted both. Unstable boots neither inflate the total nor disappear.
>
> Note the consequence: **the boot number is provisional until the gate fires.** Repeated
> failures all show the same number with a climbing `unstable` count.

**Guard:** persist only once the device has been up for **60 seconds**. A boot loop crashes
before that and never writes; a genuine reboot writes once. This also improves what the
number means. It becomes a count of *stable* boots, and any divergence from reality is
itself a signal.

Pair with a Tier 1 **unstable-boot counter**: incremented at boot, cleared when the 60
seconds elapse. Free, no flash, and it makes a boot loop visible in the next heartbeat
instead of invisible.

### C6: Fix the startup message wording (P3, trivial)
`startupText` reads "I am online for the first time! Bot started!" but `isStartup` is a RAM
flag, so it is `True` on every boot, and every restart claims to be the first.

Harmless until C5, which now prints a boot counter directly beneath it. "For the first time"
above `boot #47` is self-contradictory.

The flag's actual job is distinguishing the boot-time announcement from a post-reconnect one
*within a session*, which the wording does not reflect. Reword to "Bot started." and
"Reconnected.", and let the reset line carry the boot number.

Fold into whichever commit next touches those strings, or into E1 when the message text moves
to configuration.

---

### C7: Reconsider the maximum plausible ring (P3)
`STUCK_INPUT_MS` is 15 s, so anything shorter is accepted as a ring. The bench logged
`Doorbell ring, 13513ms` and `11480ms` from a hand-held wire, and both would have sent
alerts.

Harmless on the bench, but on production a 13-second assertion is not a ring: terminal 04
sends about 2 s, and a held button produces repeated bursts (see `G10`) rather than one long
pulse. A ceiling nearer 5 s would treat a long assertion as the fault it probably is.

Wants `G10`'s burst measurement first: if a held button really does produce one long pulse
on some installations, the current ceiling is right.

---

## D. Security

### ✅ D1: Stop leaking the bot token (P0, trivial)
Remove `print(url)` from `read_message`, or redact the token.

It currently prints `https://api.telegram.org/bot<TOKEN>/getUpdates?...` on **every poll**.
Anyone pasting Thonny output into a GitHub issue or forum thread hands over full control of
their bot.

### D2: Secrets file hygiene (P1)
Ship `secrets.example.py`, gitignore `secrets.py`, remove the tracked file from the docs
flow. Add a startup check that refuses to run on unreplaced placeholders with a clear error.

The README currently instructs users to edit a **tracked** file containing their WiFi
password and bot token.

Also add a `deviceName` field. With a bench unit and a production unit running
simultaneously against separate bots and groups, every startup message, heartbeat and log
line needs to say which board sent it, and crossing the two `secrets.py` files either
floods the real group or sends production alerts to a test channel nobody watches.

### D3: Sender authorization (P1)
Verify incoming updates originate from the configured chat before acting on commands.
Optionally maintain an allowlist of user IDs for command issuance.

In a group, **any member** can currently request the log, which contains the local IP and
network history.

Related: confirm the BotFather privacy-mode setting (`/setprivacy`). With privacy **on**,
the bot only receives commands and replies. With it **off**, it receives every message,
photo, sticker and join notification in the group, each one currently an unhandled
exception plus a WiFi teardown.

### D4: TLS posture (P3, research)
`urequests` does not verify certificates, so the bot token is exposed to a MITM on the local
path. Full verification is awkward within the Pico W RAM budget.

Minimum deliverable: document the exposure honestly in the README. Investigate whether
current MicroPython builds make pinning to Telegram's CA feasible. Treat as research, not a
committed deliverable.

---

## E. Architecture

### E1: Configuration extraction (P1)
> **Pin assignments already extracted.** The two boards use different doorbell input pins
> (GP16 vs GP18), so `doorBellPin` moved to a per-revision `board.py` ahead of schedule. The
> rest of E1 (intervals, message strings, country code, polarity, feature toggles) still
> belongs to Phase 3, and should go somewhere other than `board.py`, which is deliberately
> limited to hardware wiring.

`config.py` holding: GPIO pin, country code, all intervals, all message strings,
active-high/active-low polarity, feature toggles.

The README's "Final details" section is currently a list of *"now go edit source code"*
instructions. It should become *"edit `config.py`."*

### E2: Module split (P1, raised)
`main.py` is **1720 lines**, up from 209 at the start of this work. It was P2 when the file
was small enough that the cost of leaving it alone was theoretical. It no longer is.

**Proposed layering.** Acyclic, each module depending only on those above it. Revised from
the original list to reflect C1 and G1, which landed after the first draft: the watchdog, the
wall-clock reads, and the LED are pulled out as leaf modules so nothing reaches sideways for
them.

| Module | Holds | Depends on |
| --- | --- | --- |
| `board.py` | Pin assignments per revision *(done)* | - |
| `secrets.py` | Credentials, chat id *(done)* | - |
| `config.py` | Timings, thresholds, message strings (E1) | - |
| `wdt.py` | The watchdog and `sleep_fed`: the "do not starve the dog" primitive | - |
| `clockmod.py` | C1 anchor plus `clock_now`, `format_timestamp`, `clock_is_live`: pure time math | - |
| `led.py` | G1 LED state machine | wdt |
| `applog.py` | `logLines`, `append_to_log`, `report`, `print_log`, `log_prefix` | config, clockmod |
| `persist.py` | `state.json`, the tier rules, atomic write | applog |
| `resets.py` | Scratch registers, `read_reset_info`, verdicts | applog, persist |
| `net.py` | WiFi connect/bounce, `do_request`, backoff | config, applog, wdt, led |
| `telegram.py` | `send_message`, `read_message`, commands, ring queue, `sync_clock` | net, applog, persist, clockmod, led |
| `doorbell.py` | Input records, IRQ handlers, pulse judging | config, applog |
| `main.py` | `boot()`, the loop, wiring | everything |

**Why the extra leaves.** Feeding the watchdog is a cross-cutting primitive, not a net
concern: every long operation must feed, so `wdt.py` is a leaf everything may depend on, the
way everything may depend on `config`. That is what lets `led.py` be atomic: it needs only
watchdog-safe blink timing (`wdt`), not a sideways reach into `net`. The wall clock splits:
the pure reads and formatting are leaf math in `clockmod.py` (so `applog`'s `log_prefix` can
use them with no cycle), while `sync_clock` and `maybe_resync_clock`, which need net, persist
and report, live in `telegram.py` and set the anchor through a `clockmod` setter. Without the
split, `applog` to clock to `telegram` to `applog` would be a cycle.

`doorbell.py` deliberately does **not** send. It latches and judges; delivery is
`telegram.py`'s job. That is what keeps the graph acyclic, and it already reflects how B1
and B6 are written.

`led.py` deliberately does **not** decide *when* to change state. It renders states and
runs blink bursts; the policy (connecting while joining, alert on delivery) stays with the
callers in `net.py` and `telegram.py`. Same principle as doorbell: mechanism here, policy
there.

**Functions, not classes: decided.** MicroPython charges for every class and instance, and
there is exactly one of each thing here: one input list, one queue, one log. Classes would
buy testability this codebase already has by other means, at a cost in RAM on a part with
176 KB free. Modules give the namespace separation without the overhead.

Not to be revisited during the split. If a second instance of something ever genuinely
appears (a second board driven from one Pico, say) that is the moment to reconsider, and
not before.

**Measure the cost.** More modules means more module objects, more globals dictionaries,
and more import-time parsing. C3's heartbeat now reports free memory, so take a reading
before and after rather than guessing. If the cost is material, `.mpy` precompilation with
`mpy-cross` removes the parse step and most of the source overhead.

**Sequence.** All eight test files currently reach into `main.py` by AST extraction and will
break. That is F1's remaining work: rewrite them as plain imports, one module at a time,
moving code only once the tests for it import cleanly. Tests first, then the move. That is
the opposite of how it is tempting to do it.

**Watch for implicit globals.** The main loop runs at module scope today, so assignments
like `lastPassTicks = ...` mutate globals without declaration. Every one becomes a bug the
moment it moves inside a function. There are several.

Roughly: `config.py`, `wifi.py`, `telegram.py`, `applog.py`, `doorbell.py`, and a thin
`main.py`.

> **Caution:** the current `while True` loop runs at module scope, so assignments like
> `lastLogCheck = ...` mutate globals implicitly. Every one of these becomes a genuine bug
> the moment the loop is wrapped in a function. This refactor must be done deliberately,
> not mechanically, and **after** F1.

### E3: Notifier abstraction (P3)
A minimal `send(event)` interface so Telegram becomes one backend among several. Enables E4
without further surgery.

### E4: MQTT / Home Assistant backend (P4)
Dramatically lighter and lower-latency than TLS HTTP on this chip, and what the
home-automation audience actually wants. With HA discovery the doorbell becomes a
first-class entity.

### E5: Long polling (P3)
`getUpdates` with a `timeout` parameter reduces request volume, but it **blocks**, which is
only safe once B1 has decoupled input capture from the loop. **Strictly ordered after B1.**

### E6: Do not offload networking to core 1 (decision record)
The RP2040 has two Cortex-M0+ cores and MicroPython exposes core 1 via `_thread`. Moving the
network work there is a natural idea. **Rejected.**

**It solves a problem B1 already solves, and solves it worse.** The reason to offload
networking is to stop it blocking doorbell detection. A pin IRQ does that in about ten lines,
and a hardware interrupt preempts everything, including a running thread, so the input is
captured regardless of what either core is doing.

It buys nothing else. Notification latency is dominated by the TLS handshake, which is no
faster on core 1. There is no other work for core 0 to do meanwhile, and the device handles
one event type.

Supporting reasons:

- `_thread` on rp2 is documented as experimental, and running the network stack on core 1 is
  a known trouble spot: lwIP and the cyw43 driver are not thread-safe in MicroPython. The
  v1.29.0 release notes list thread fixes for the rp2 port, so the area has been actively
  broken.
- MicroPython uses a GIL, so two threads interleave rather than running in parallel.
  Concurrency, not parallelism: considerably less than the hardware implies.
- **Watchdog interaction, the one that would actually bite.** If core 1 wedges while core 0
  keeps feeding the watchdog (B2), the device never resets and the fault is invisible.
  Avoiding that needs a cross-core liveness check: more machinery than the thing being
  built.
- Core 1's stack comes from the same 264 KB heap already shared with TLS buffers.

**Where it would be legitimate:** genuine concurrent work, such as audio streaming to a SIP
extension. Not for stopping one HTTPS POST from blocking a pin read.

---

## F. Testing & tooling

### F1: Host-side stubs (P2)
Fake `machine`, `network`, `rp2`, `urequests` modules so the logic runs under CPython.

This is the enabler for the whole category, and it is what makes E2 safe to attempt.

### F2: Unit tests (P2)
Cover:

- the update parser, against **real captured payloads** for all four chat types
- junk cases: no `text` key, service updates, `my_chat_member`
- the migration response shape
- the command parser (`/log`, `/log@bot`, `/log args`, case variants)
- the log chunker at the 4096 boundary
- the connection state machine

### F3: CI (P3)
> **Partly done.** `tools/check.py` already runs everything a CI job would: trailing
> newlines, byte-compilation, roadmap structure and all eight suites, exiting non-zero on
> failure. What remains is wiring it to a GitHub Actions workflow and adding `ruff` and an
> `mpy-cross` compile check.

Ruff lint, run tests, `mpy-cross` compile check to catch syntax errors before they reach a
board.

### F4: Style pass (P3)
PEP 8 naming, drop parenthesized conditions, remove dead code (`startupTime`, trailing
`pass`, unused loop variable), fix the `chatId` parameter shadowing the module global.

**Do this last, as a single commit,** so it doesn't obscure the substantive diffs.

---

## G. Hardware & UX

### 🔬 G1: LED state machine (P2)

> Bench-verified on hardware (boot #37): solid on at rest when connected; a delivered ring
> shows a double-blink then returns to solid (a rejected transient shows nothing, so the
> alert is correctly gated to real deliveries); dropping the AP produced the ~1 Hz
> connecting blink, and restoring it went connecting blink to 3-flash confirmation to solid
> on. That reconnect cycle is the path the old inversion bug lived on, and the LED tracked
> reality throughout, blinking while down, solid only once actually back up. The error blink
> (`LED_ERROR`) cannot be triggered without a fatal setup fault and stays test-covered.
> `tests/test_led.py`, 28 assertions. Not yet on production.

Distinct patterns for connecting / connected / error / alert-sent.

A single source of truth: the steady state (off, connecting, connected, error) lives in
`ledState` and is rendered by `set_led_state`; nothing sets a bare level any more. Discrete
events (the connect confirmation, a delivered ring, the fatal fast blink) are short bursts
that restore the steady state when they finish. Blink bursts feed the watchdog through
`sleep_fed`, so a pattern cannot outlast the timeout. The connecting blink is driven by the
connect loop, which already polls every `WIFI_POLL_MS`, toggling once per pass for a roughly
1 Hz flash without a timer. `error_halt` keeps its unfed fast blink deliberately: a fatal
setup fault should let the watchdog escalate rather than be held off forever.

Fixed the inversion: the error handler called `wlan.disconnect()` and then `led.on()`,
leaving the "connected" indicator lit on a disconnected device. It now sets the off state
after disconnecting, and the next loop pass drives the LED back through connecting to
connected on its own.

### G2: Battery telemetry (P4)
VSYS via ADC3, with the GPIO25-high sequencing the Pico W requires. Report in the heartbeat,
warn on low.

Deferred: no hardware to read on V1.1.

### G3: Input signal conditioning (P2)
Document and handle the AC-bell case, where a single PC817 produces a pulse train at mains
frequency rather than a clean level.

✅ **Resolved for this hardware.** The deh0511 pinout for the 7630 Wohntelefon documents
terminal 04 as a bell-signal output at approximately 5 V **DC**. There is no AC pulse train
to coalesce, and the bridge-rectifier inference from mikrocontroller.net was correct.

G3 therefore stays open only as a **generic warning for other users**, whose bells may well
be AC. It is not something this installation needs to handle.

**Bounce is confirmed by prior art, not merely suspected.** Bracke reports having had to
solve bell-input debouncing among his first problems on the same TwinBus system. B1's
debounce is therefore mandatory rather than defensive.

Interacts directly with B1's debounce parameters. Decide: solve in software (pulse-train
coalescing) or recommend an RC stretcher on the opto output.

### G4: Pico 2 W support (P3)
Verify and document. It is the board most people would buy today.

### G5: Preserve the deep-sleep path (P3, architectural constraint)
A future battery build will want `lightsleep`/`deepsleep` with IRQ wake, which is
incompatible with a busy main loop that assumes it runs forever.

Nothing to build now, but E2's module split must keep the event loop **swappable** rather
than baking `while True: sleep(1)` into the architecture. Cheap to honour now, expensive to
retrofit.

> On battery, unexpected power loss goes from rare to routine, which raises the value of
> B6's flash snapshot. Another reason to build the mechanism now even if it triggers rarely.

### G6: RUN pin noise immunity (P1)
Fit **100 nF from RUN to GND**, as close to the pin as layout allows, plus a **10 kΩ
pull-up from RUN to 3V3**.

The RP2040's internal RUN pull-up is weak (~50 kΩ), leaving a high-impedance node with a
length of wire attached and little noise immunity.

> **Note on evidence.** This item was originally justified by the observation that re-mating
> the reset connector rebooted the board. That observation has since been set aside as an
> artifact of connector travel (see hardware queue item 6) and is *not* evidence for
> electromagnetic pickup. G6 remains worth doing as standard practice for a RUN line with
> wire attached, but it is **precautionary**: nothing currently confirms RUN as the cause,
> and C4 should decide whether it justifies a board revision.

The capacitor gives roughly a 5 ms time constant against the internal pull-up: long enough
to swallow a transient, short enough not to interfere with the button or startup. The
external pull-up lowers the node impedance about 5×, so a given injected charge moves the
voltage far less. Together they are meaningfully better than either alone.

Prototype on protoboard first, then a carrier board revision. Also check whether the reset
cable runs near the doorbell wiring or mains: rerouting may help as much as the capacitor.

> This establishes that the RUN line *can* be disturbed trivially. It does not yet prove
> the mains-switching reboots share that mechanism: plugging a connector is a physical
> disturbance, a light switch is an electromagnetic one. C4 closes that gap - `chip=RUN` on
> a reboot coinciding with a light switch is the confirmation.

### ✅ G7: Characterise the ring pulse (P1, was blocking B1)
Measure how long the optocoupler output actually stays asserted during a ring.

✅ **Measured.** Terminal 04 idles at ground and goes **high to 5 V for approximately two
seconds** on a ring: a clean digital square wave, not an edge and not a pulse train.

Consequences:

- **Ring loss is rare, not routine.** A 2 s assertion against a 1 s poll is normally caught.
  A ring is only missed if a blocking call spans the whole pulse: a ~3 s TLS handshake once
  a minute gives roughly a 1-2% miss rate per ring. A handful per year, not the systematic
  loss feared earlier. B1 remains worth doing; it is not an emergency.
- **Polarity already correct.** Idle low, active high → `IRQ_RISING`, matching the existing
  `PULL_DOWN` and `pressed = 1`.
- **Both buttons assert it** (see G9), so the 2 s level says "someone rang", not which door.

Original reasoning, retained for context:

**Semantics confirmed, duration still unknown.** The deh0511 pinout documents terminal 04
as a bell-signal output at roughly 5 V DC, matching Bracke's description of it as an
ordinary button press. It is a level, not a brief edge, which is better for the current polling loop
than the `tuxuser` project's "ring **pulse**" wording implied.

What remains unmeasured is **how long it stays asserted**, which is the number that decides
whether a one-second poll, a five-second post-press sleep, and multi-second blocking TLS
calls can miss it.

> **Measure under load, not open-circuit.** A signalling output is not necessarily a stiff
> supply. 5 V through the 180 Ω series resistor draws roughly 21 mA into the PC817: a
> healthy LED drive, but a substantial load for a signal pin. If the level sags under it,
> that is an argument for raising the resistor.

> **A missed ring is silent.** No error, no log entry: just someone at the door who leaves.
> Level semantics make this less likely than a pulse would, but not impossible.

Two ways to measure, either is fine:

- Scope or logic analyser on the optocoupler output during a real ring.
- A tight-loop MicroPython script printing the pulse width in milliseconds. No equipment
  needed; five minutes on the bench with a jumper standing in for the bell.

**Sets B1's debounce window**, so it wants doing before B1 is written. Also feeds G3.

✅ **Both buttons assert terminal 04**, confirmed by testing. See G9 for the consequence.

While measuring, also capture **whether terminal 04 and the ED line assert simultaneously or
with an offset**. That sets the correlation window G9 needs.

### G8: Do not power the Pico from the bus (P3, decision record)
Terminal 05 on the 7630 carries the +24 V bus supply. It is tempting: if the reboots turn
out to be supply-related, powering from the bus would remove the USB adapter from the
picture entirely.

**Recorded as rejected, and now settled by the numbers.** The TwinBus system handbook gives
the 17573 power supply's output to the system bus as **15 V DC at 200 mA**. A Pico W's WiFi
TX bursts alone are 250-300 mA: the transmit peak exceeds the entire system's DC budget.

Bracke tried precisely this on the same system, a DC-DC converter off the bus, and reported
that the intercom stopped working for lack of power; he moved to a separate supply. That is
exactly what these figures predict. In a multi-party building the margin belongs to
everyone's doorbell, not just this one.

Written down so it does not resurface as an obvious idea.

### G9: Distinguish building entrance from flat door (P3, deferred)
Terminal 04 asserts for **both** the building door station and the flat's own Etagendrücker,
confirmed by testing. Notifications therefore conflate two different events: someone at the
building entrance, versus someone already inside at the flat door, usually a neighbour or a
delivery that has already been let in.

TwinBus itself distinguishes them, signalling each with a different ring tone, so the
information exists on the system; it simply is not visible on terminal 04.

**Approach.** The deh0511 pinout documents terminals 01 and 06 as *Etagentaster gegen GND*,
so the flat's button has its own connection. A second optocoupler there gives the
discriminator:

| Terminal 04 | ED input | Meaning |
| --- | --- | --- |
| asserted | asserted | Flat door |
| asserted | idle | Building entrance |

Costs one optocoupler, one GPIO, and a time-window check in software.

**Cautions.**

- This senses a *switch line*, not a signal output, so it adds load to the Etagendrücker
  circuit. The system handbook specifies bell buttons must not exceed 10 Ω contact
  resistance, which suggests Ritto cares about impedance on that path. Keep the tap
  high-impedance.
- The correlation window depends on whether 04 and ED assert simultaneously or with an
  offset. Measure during G7, since both buttons are to hand.

Interacts with B1: two latched inputs rather than one, and the discrimination happens after
both have been sampled rather than in either ISR.

### G10: Confirm the TwinBus burst interval (P2)
Terminal 04 does **not** track the button: a ~6 s press produces one pulse, so the system
debounces internally and emits its own fixed signal. That removes the risk of tuning
`MIN_PULSE_MS` too high: a quick jab should still produce a full-width pulse.

But a held button reportedly produces repeated bursts: tone, pause, tone. If the burst
interval exceeds `ALERT_LOCKOUT_MS` (5 s), a visitor leaning on the button generates several
notifications.

No oscilloscope needed: the firmware is the instrument. `Doorbell ring, NNNNms` gives the
width, and successive entries give the interval. Hold the real doorbell for fifteen seconds
and read the log.

---

## H. Documentation

### ✅ H1: Broken links and typos (P1, trivial)
Done, except where a fact is missing that only the author has.

- ✅ Malformed SMD gerber URL: the parenthesis inside the link is removed.
- ✅ "EF2 file" → **UF2**, both occurrences.
- ✅ **The 180 Ω / 185 Ω "inconsistency" was not one.** 180 + 5.1 = 185.1, so the two
  resistors are a series pair giving the 185 Ω the datasheet note refers to. The original
  review called it a contradiction and guessed 5.1 kΩ was intended. Both guesses were wrong,
  and the arithmetic would have settled it in a second. The parts list now says so, since it
  misled at least one reader.
- ✅ Grammar: "must  before", "may chose", "running on Germany".
- ⚠️ **Both gerber links still name the same file.** The syntax is fixed, but the SMD link
  may point at the through-hole archive. Only the author can tell.
- ⚠️ **Heading "V 1.2 schematics" shows `schematicsV01_1.png`.** Possibly just a filename,
  possibly the wrong image. Not guessed at.

### H2: Setup rewrite (P2)
Document the DM, group, supergroup and channel paths properly now that all four are
supported. Include the supergroup migration caveat and the BotFather privacy-mode setting.

### H3: Repo hygiene (P3)
Tagged releases matched to PCB revisions, a CHANGELOG, and a troubleshooting section built
from the failure modes catalogued here.

### ✅ H4: Architecture document (P2)
`docs/ARCHITECTURE.md` describes how the firmware works and why: the state tier model, the
boot sequence, the HTTP request layer, the on-device file layout. Kept separate from the
README (a build guide) and from this file (a plan). Updated alongside each commit that
changes behaviour it describes.

### ✅ H5: Ritto/TwinBus safety warning (P2)
The README treats this as a standalone doorbell project. It is not: the TwinBus is a
building-wide system fed from a PSU in a shared area. Bracke's writeup warns that mistakes
connecting to a Ritto installation can damage the whole building's system, not just the
endpoint.

✅ Added to the README, immediately after the existing disclaimer and ahead of the wiring
instructions. The same caution appears
community-sourced: a mikrocontroller.net poster with a logic analyser held off from probing
his own installation because it served four parties and a mistake would have taken out
everyone's doorbell.

Terminal conventions worth documenting alongside it, since they recur across Ritto
manuals: `a`/`b` are the two bus wires, `ED` is *Etagendrücker* (the flat's own door
button), `TÖ` is *Türöffner* (door opener).

The board-level points on the 7630 Wohntelefon are reverse-engineered rather than official.
Per the deh0511 pinout, the ones this project touches are **04** (bell signal output, about
5 V DC) and **03**/**09** (ground). **05** carries +24 V bus voltage: see G8 for why it
should be left alone. Several points on that connector remain undocumented. Credit
`deh0511.de/twinbus` as the source rather than reproducing the table wholesale. Also worth linking the prior art
(`deh0511.de/twinbus`, beechy.de, `tuxuser/ritto_doorbell`) as pinout references.

---

## I. Persistence & state

### I1: Tiered state model (P1)
Split persisted data by write frequency, and **enforce the tier in code**, not by convention.

| Tier | Medium | Contents | Write frequency |
| --- | --- | --- | --- |
| 0 | RAM | Rolling log, alert counters, live queue | Never persisted |
| 1 | Watchdog scratch registers | Reset reason, boot count, crash code | Free, zero wear |
| 2 | Flash (`state.json`) | Migrated chat ID, NTP epoch anchor, config overrides | Transition-driven; a handful per device lifetime |
| 3 | Flash (snapshot) | Queue contents | Rare, conditional only (see B6) |

**Tier 1 detail:** the RP2040 has eight 32-bit watchdog scratch registers that survive both
a soft reset and a watchdog reset (not power loss): 32 bytes of free, zero-wear,
reset-surviving storage, reachable via `machine.mem32` at the watchdog base.

> ✅ **Verified on hardware.** Scratch 2 and 3 behave as expected on v1.23.0. Note that any
> hardware reset clears them, RUN included, not only power loss, as first assumed.

**The one rule that matters: never write flash on a timer.** Everything else follows.

### ✅ I2: Atomic write helper (P1)
One function, one file, one write. Serialize all Tier 2 state together, write to
`state.tmp`, `os.rename()` over the target. Rename is atomic under littlefs, so a power cut
mid-write cannot leave corrupt state.

Read-before-write comparison so an unchanged value never triggers an erase.

Costs one extra erase per write: irrelevant at these frequencies, and it buys integrity.

**Do not rewrite `secrets.py`.** Runtime state belongs in a separate file; rewriting a source
file the user also edits by hand invites a merge conflict on a microcontroller.

### 🔬 I3: Wear instrumentation (P2)
Keep a monotonic write counter inside the state file and report it in the heartbeat (C3).

This makes wear **observable** rather than theoretical. If the counter climbs faster than
expected, there is a bug in the write discipline, and it surfaces in month one instead of
year three.

✅ **Delivered with C3.** The bench unit reads **38 writes** across its whole life: roughly
one per stable boot over 30 boots, plus a few queue snapshots from outage testing. Against
~100,000 cycles that is nothing, and the bench unit has taken far more abuse than production
ever will.

### ✅ I4: Filesystem verification (P3)
Confirm whether the build uses littlefs2 or FAT, and record it in the troubleshooting
section. It changes the endurance envelope by an order of magnitude.

---

## J. Deployment & updates

### J1: Over-the-air updates (P3)
Pull firmware from a URL and self-update, via `mip` or a small self-updater reading GitHub
raw. Needs: a version marker, an atomic swap (I2's temp-file-and-rename pattern applies
directly), and a rollback path if the new build fails to boot.

> Raised in the original review and then dropped when this roadmap was first written. It is
> restored here because the two-device workflow makes it concretely valuable: promoting a
> tested build currently means physically pulling the production Pico out of the entryway.
>
> Independently motivated by prior art: Bracke added HTTP-based firmware updates to his
> TwinBus integration specifically to avoid opening the intercom case for every change.

Deliberately **not** in Phase 1. An OTA path that can brick the device is worse than no OTA
path, and the rollback story depends on C4's reset-cause detection to know a new build
failed. Sequence it after C4 and B2 are proven on hardware.

---

## K. Platform tracking

### K1: Adopt `machine.mem_backup()` for Tier 1 (P3)
C4 writes the boot counter and magic word into watchdog scratch registers via raw
`machine.mem32`, using a register map taken from the datasheet and not yet confirmed on
hardware. MicroPython v1.29.0 adds `machine.mem_backup()`, a supported API for
hard-reset-surviving memory on rp2 and six other ports, returning a writable memoryview.

Adopting it would remove our dependence on an unverified register map. Two constraints:

- **Version gating.** The project targets other people's boards, many on older builds, so
  this has to be runtime-detected: use `mem_backup()` when present, fall back to `mem32`
  otherwise: rather than a hard requirement.
- **Possible conflict.** If `mem_backup()` is backed by the scratch registers C4 already
  uses, the two will collide on any build that has it. Verify before upgrading either board
  past v1.28.

Blocked on the firmware upgrade, which is itself blocked on identifying the reboot cause.

### K2: Track rp2 port improvements (P4)
v1.29.0 brings roughly a 10% rp2 performance gain and fixes for threads, lightsleep and
UART IRQ latency. The lightsleep fixes are relevant to G5 and the future battery build.
Nothing to do until the upgrade happens; recorded so it is not rediscovered later.

---

## Sequencing

Two axes: sections say what *kind* of work an item is, phases say *when*. An item's
priority tag and its phase are independent - `E2` is P1 but waits on the production
milestone below, because it should not happen before the current tree has proven itself in
place.

Phases are numbered in the order they happen. Restructuring was originally third and
correctness second; bench findings reversed them, so the numbers were swapped to match
rather than left as a trap for anyone reading top to bottom.

### ✅ Phase 1, Stop the bleeding, complete

✅ `D1` → ✅ `A5` → ✅ `A3` → ✅ `I2` → ✅ `B3` → ✅ `C4` → ✅ `C5` →
🔬 `A9` → 🔬 `B1` → 🔬 `B2` → 🔬 `B4` → 🔬 `B6` → 🔬 `B8` →
🧪 `A6` → 🧪 `A7` → 🧪 `C3`

Production runs everything up to `C5`. The rest is on the bench: most of it verified, three
items still under test. Each 🧪 item says what it is waiting on.

Five items were pulled forward from later phases by bench findings rather than plan:

| Item | Why it moved |
| --- | --- |
| `A9` | A dropped network mid-request hung `urequests` and the watchdog reset the board |
| `B4` | `wlan.connect()` re-issued ~200 times left the stack associated but carrying nothing |
| `A6` | `/log` stopped working once the log passed Telegram's 4096-character limit |
| `A7` | Every reset replayed Telegram's backlog, re-running old commands |
| `B8` | WiFi power save was a live suspect for the timeout failures |

The device no longer loses presses, no longer fails silently, and reports its own health.

### ⏳ Production milestone: before Phase 2

Production runs the **C5 build**. Everything from `B1` onward is bench-only.

1. Finish the bench soak; take the free-memory trend from `C3`.
2. Promote the current tree to production.
3. Let the reboot dataset accumulate: a week or two.

Nothing in Phase 2 or 3 should start before this. The reboot investigation is the reason `C4`
exists, it only produces data on the production unit, and a large refactor on top of code
that has never run in place would confuse both.

### Phase 2: Restructure

`F1` → `E2` → `F2` → `F3`

`E2` is the highest-priority item left: `main.py` is 1720 lines, and every correctness
change in Phase 3 makes it longer. Splitting first means those changes land in files that
make sense.

**Tests before the refactor, not after.** All eight test files reach into `main.py` by AST
extraction and break on the first move. `F1` rewrites them as plain imports, one module at a
time.

### Phase 3: Correctness

`A1` · `A2` · `A4` · `E1` · `D2` · `D3` · `B5` · `I1` · `H1` · `C6`

`C1` was originally scoped into this phase but was pulled forward and is now bench-verified
(see its entry above); it is no longer part of the remaining Phase 3 work.

The device already behaves correctly *here*: this deployment uses a channel, which the
existing parser handles. Most of this phase is **portability**: making it correct for the DM
and group paths the README documents, for other people's boards.

Suggested order within the phase:

| Group | Items | Why together |
| --- | --- | --- |
| Update handling | `A1` · `A2` · `A4` | One rewrite of the parser and dispatch |
| Configuration | `E1` · `D2` | Both move values out of source; `D2` decides whether `secrets.py` is renamed |
| Hygiene | `D3` · `B5` · `I1` · `H1` · `C6` | Small, independent |

`C1` (real timestamps) was the one time item here and is already done: it was pulled forward
because `ticks_ms` timestamps made every other diagnosis harder, and the queue already
carried an epoch field waiting for it. Its early completion is why this phase is now purely
update handling, configuration, and hygiene.

### Phase 4: Polish and roadmap

`B7` · `C2` *(finish)* · `G1` · `I3` · `E3` · `E5` · `A8` · `D4` · `F4` · `G4` · `G10` ·
`H2` · `H3` · `H5`

Then, gated on hardware or a proven baseline: `J1` (needs `C4` and `B2` proven in the
field), `G6` (needs the reboot data to justify a board revision), `G5` · `G9` · `G2` · `E4`
(all future hardware).

### Dependency edges worth respecting

```
F1 ──▶ E2 ──▶ F2          tests before the move
E2 ──▶ G5                 the loop must stay swappable for deep sleep
C1 ──▶ queue timestamps   restored rings cannot be aged without a clock
C4 ──▶ J1                 rollback needs to detect a failed boot
I2 ──▶ J1                 atomic swap reuses the same pattern
B1 ──▶ E5                 long polling is only safe once input is decoupled
B1 ──▶ G9                 a second input is another entry in the latch list
D2 ──▶ E1                 whether secrets.py is renamed decides where config lands
```

---

## Appendix: Flash wear analysis

The Pico W carries 2 MB of QSPI NOR (W25Q16-class): **4 KB erase sectors**, **~100,000
program/erase cycles per sector**, 20-year retention. MicroPython's firmware occupies
roughly 600-700 KB, leaving ~1.3 MB, about 330 sectors, for the filesystem.

Risk is almost entirely about **write frequency**, not write size: a 40-byte state file and
a 4 KB one cost the same, because the erase granularity is a sector either way.

Pessimistic floor, assuming **no** wear leveling and every write hitting the same sector:

| Write pattern | Sector lifetime |
| --- | --- |
| 1 write per press, 20 presses/day | ~13 years |
| 10 writes/day (log flush) | ~27 years |
| **1 write/minute** | **~69 days** |
| 1 write/second | ~28 hours |

**Event-driven writes are a non-issue by a wide margin. Scheduled writes are lethal.**

Real-world figures should be better: MicroPython's RP2 port uses **littlefs2**, which does
dynamic wear leveling: allocating from a rotating pointer across free blocks rather than
rewriting in place. But verify the build first (I4); if it is FAT there is no leveling and
the table above is the real number. Note also that littlefs does dynamic but **not static**
wear leveling, so blocks holding rarely-changed data never rotate into the pool, and the
metadata pair stays comparatively hot. Design to the floor; treat leveling as headroom.

### Why the discipline matters even though a Pico W is cheap

Worn NOR flash **does not fail cleanly**. It begins failing *writes* while reads still
succeed. The result is a device that boots, runs, looks healthy, and silently stops
persisting state: the exact failure class this entire refactor exists to eliminate.

### Two constraints this imposes on the design

1. **Flash writes stall the CPU and disable interrupts.** A sector erase blocks for tens of
   milliseconds, worst case a few hundred. Therefore: B2's watchdog timeout needs margin for
   a worst-case erase, and **never write flash from inside an ISR**.
2. **A doorbell edge arriving during a write is not lost.** The RP2040 latches GPIO edge
   events in hardware, so the interrupt fires late rather than never: provided B1's ISR
   stays minimal (set a flag, return).

---

## Open questions

- **Filesystem type** on the target build: littlefs2 or FAT (I4). Changes the endurance
  envelope by 10×.
- **Scratch register availability** in the specific MicroPython build (I1, Tier 1). Confine
  to 0-3 and verify.
- **AC vs DC bell signal** (G3): determines whether debounce is software pulse-coalescing
  or an RC stretcher on the opto output.
- **BotFather privacy mode** on *both* bots (D3): must be set identically. A mismatch makes
  the bench unit hit the `channel_post` crash path constantly while production looks fine,
  or the reverse, and the difference looks like a firmware bug.

### Hardware verification queue

Open items for the bench unit, none yet answered:

1. ✅ **MicroPython version parity: done.** Both boards now run **v1.23.0** (2024-06-02).
   The prototype was brought up from v1.19.1 to match production, not the reverse:
   production is the instrument for the reboot dataset, and reflashing it would have reset
   an uncollected baseline while adding a second changed variable alongside C4.

   Side effect: flashing the `.uf2` erased the prototype's filesystem, so it starts with no
   `state.json`. That is a clean first-boot condition for testing I2: expect
   `cause=PWRON_RESET`, `boot #1`, `cold`, and no state file until something writes one.

   This also **retires firmware age as a reboot hypothesis**. It was raised on the
   assumption production might be the older board. June 2024 is recent, and the early Pico W
   lwIP problems were fixed well before it. Remaining candidates: supply sag and RUN-pin
   pickup.

   The `ujson`/`uos` fallback stays regardless: v1.23.0 provides both names, but the
   project targets other people's boards too.

   **Not upgrading to the current release yet.** v1.29.0 (2026-08-24) is the latest, with
   v1.28.0 (2026-04-06) before it. Both are declined for now:

   - Production is the measuring instrument for an unresolved fault with no baseline and no
     confirmed cause. Changing the runtime adds a variable to an experiment that already has
     too many: if the reboots stop, the cause would be unattributable between firmware, the
     disconnected cable, and chance.
   - v1.29.0 is days old and a large release (a new port, a new `machine.mem_backup()` API
     across seven ports). A wall-mounted appliance wants a version with field exposure.

   **Sequence:** prototype to v1.23.0 now for parity → identify the reboot cause → then
   upgrade as a deliberate experiment, prototype first with a multi-day soak, production
   after. Prefer v1.28.0 or a v1.29.x point release over v1.29.0 at that time.

   ⚠️ **Check before any upgrade past v1.28:** v1.29.0 adds `machine.mem_backup()`, which
   exposes hard-reset-surviving memory on rp2. If it is backed by the same watchdog scratch
   registers C4 writes to, MicroPython may use or clear them. The magic-word check would
   catch it, every boot would report as cold, but that is a silent degradation, not an
   error. See K1.
2. ⚠️ **Prototype doorbell input pin: the boards differ.** Production uses **GP16**, the
   prototype uses **GP18**, confirmed from the firmware saved off the prototype before
   reflashing.

   **This blocks bench testing.** `doorBellPin = 16` is a constant in `main.py`, so flashing
   the current tree to the bench unit would leave the input on the wrong pin, and fail
   silently, since nothing would ever assert it.

   ✅ **Resolved.** Pin assignments now live in `board.py` on the device, copied from a
   per-revision file in `boards/`. `main.py` holds no pin defaults and refuses to start
   without a complete definition: an earlier proposal to put the pin in `secrets.py` was
   rejected, correctly, because a GPIO number is not a secret. See `ARCHITECTURE.md`.
3. ✅ **Filesystem type: littlefs2.** `os.statvfs('/')` returns 4096-byte blocks, 212
   total, 202 free. The block size matches the flash sector, which is characteristic of
   littlefs2 and consistent with the rp2 port's long-standing default. The atomic-rename
   guarantee in `ARCHITECTURE.md` holds. Strong inference rather than proof; the decisive
   test is case sensitivity, since littlefs distinguishes `AAA` from `aaa` and FAT does not.

3c. ✅ **Memory baseline: 178,480 bytes free** after boot with WiFi up. Reference point for
   the leak watch.

3b. ✅ **Ring pulse width: measured.** Terminal 04 idles at ground and goes high to 5 V for
   about two seconds. Clean digital square wave. See G7 for what follows; B1 is unblocked.
4. ⚠️ **The bench unit is a poor model for the reboot fault.** The battery on VSYS absorbs
   the supply dips production is exposed to, so it is immune to that hypothesis outright. On
   RUN it is **less exposed, not immune**: both boards carry a soldered button, so both have
   a RUN net; production's simply fans out further, to a second button on wires through a
   connector. A clean bench run therefore says little about the reboots, and G6 cannot be
   validated there: the capacitor can be fitted, but there is no fault to suppress. Reboot
   diagnosis is production-only, which is the main argument for promoting C4 there early.

   The `chip=RUN` check is unaffected: pressing the soldered button pulls RUN low the same
   way, so it still validates the CHIP_RESET bit map.

5. **Prototype reboot behaviour on both power modes.** The VSYS jumper allows switching
   between battery-backed and direct-USB. Production is direct-USB with no battery, so the
   direct-USB mode should reproduce its behaviour; a battery-backed prototype that never
   reboots proves nothing about production, because the cell masks exactly the dips in
   question. Run both on the same socket as production and switch the offending light.
6. **Charger module load-sharing.** Whether the load runs from USB while charging, or hangs
   off BAT+ with the cell charging and discharging simultaneously and held near 4.2 V.
   Affects cell longevity and what "on battery" means as a test condition.
7. 🔬 **RUN pin pickup: elimination test running on production.** The external reset cable
   is disconnected on the production unit, firmware unchanged, so the board carries exactly
   one changed variable. Record the disconnect date and the prior reboot rate - "none since"
   only means something against a baseline. If the trigger is reproducible (switching the
   offending light), test actively rather than waiting.

   **A negative result does not exonerate RUN**, and less than previously stated. Production
   carries a reset button on the carrier board *as well as* the external one, so pulling the
   cable leaves a second button and its trace still on the net. This is a partial
   elimination.

   Two buttons on one net also doubles the exposure to a **degrading tactile switch**. A
   contaminated switch can close spontaneously without mechanical provocation, which the
   knock testing would not have revealed. Bisect by lifting the on-board button if C4
   reports `chip=RUN`.

   ✅ **Superseded by detection.** With C4 on production and a RUN reset confirmed to report
   `chip=RUN` there, the question is answered by the next labelled reboot rather than by
   elimination. The cable stays connected. Retained below for context.

   **No positive control.** Deliberate attempts to provoke a reboot by switching the light,
   with the cable *connected*, produced nothing. An elimination test needs the fault to be
   demonstrable in the known-bad configuration first, so "no reboots with the cable off"
   would currently be uninterpretable. A handful of failed attempts is weak evidence either
   way: at a 5% per-event rate, twenty attempts still miss entirely 36% of the time.

   **Watch for recall bias in the original correlation.** A reboot immediately after a
   switch flip is memorable; one at 3am is not. Telegram timestamps every startup message,
   so reboot times are already recorded even before C1 lands: enough to check the
   correlation against a written log of switching events rather than against memory.

   **Characterise the load.** Inductive and electronically-ballasted loads (compressors,
   pumps, LED and fluorescent drivers) produce far worse switching transients than a
   resistive lamp. If the trigger is an LED fixture, driver inrush is a likelier mechanism
   than the switch contact.

   **The connector observation is a red herring. Set it aside.**

   Mating *and* un-mating the reset connector both reset the board, most of the time,
   without the button being pressed. Two hypotheses were built on this and both failed:

  , *Poor noise immunity on a high-impedance RUN node*, withdrawn. Symmetric behaviour in
     both directions of travel is not what charge injection looks like.
  , *Marginal contact, disturbed by vibration*, ruled out by direct test. Knocking the
     wall, the casing, the carrier board and the connector itself, and moving the seated
     connector side to side, produced **no** reboot. The contact is sound.

   What remains is unremarkable: while pins are making or breaking, RUN is briefly shorted or
   left floating, and the board resets. That requires the connector to be *in motion*, which
   never happens in service. The observation therefore says nothing about the in-service
   reboots in either direction, and no further hypotheses should be built on it.

   **Guessing is exhausted; measure instead.** C4 answers the question directly:
   `chip=POR/BOD` means the supply dropped, `chip=RUN` means the RUN line was pulled low.
   One labelled reboot settles what three rounds of hypothesis have not.

   Remaining candidates: **supply sag** (leading by elimination, and still untested: the
   production USB adapter has never been swapped) and **EM pickup on RUN** (possible, no
   evidence either way).

   The running disconnect test loses most of its value now that marginal contact is ruled
   out, but it costs only the reset button, so it can continue until C4 is deployed.

   **Plan: keep the cable disconnected for now, then reconnect it when C4 reaches
   production.** Reset-cause capture is purely observational: it labels reboots, it cannot
   cause or prevent them, so it does not confound anything. But it changes which experiment
   is worth running: direct measurement beats elimination. One labelled reboot reading
   `chip=RUN` or `chip=POR/BOD` settles the question, where weeks of silence against no
   baseline does not. With no positive control, silence is precisely what would otherwise
   need interpreting.

   C4 waits on bench verification rather than on this experiment, because it arrives
   alongside A3/A5/B3/I2, which *do* change behaviour.
8. ✅ **Test target chat type: resolved: a channel, not a group.** An earlier note here said
   group; that was wrong and is corrected. Production is a Telegram **channel**.

   This makes the existing code correct for this deployment. Channels deliver `channel_post`,
   which is exactly what the parser handles, so **`/log` works**: as originally reported -
   and only channel admins can post, so the unauthenticated-command concern in D3 is far
   milder than it would be in a group.

   It also explains the `?offset=X?chat_id=Y` URL working: Telegram tolerates it, and
   `chat_id` was never a `getUpdates` parameter anyway.

   **A1 is therefore portability work, not a bug fix.** The parser is right for a channel and
   wrong for the DM and group paths the README documents, so it still matters for other
   users: just not urgently for this installation.
9. **USB adapter quality on production.** A one-minute swap for a known-good supply is the
   cheapest test of the brownout hypothesis and needs no hardware revision. **Raised in
   priority** by the failed reproduction attempt: a cheap adapter sagging under a mains
   transient fits the symptom as well as RUN pickup does, and remains untested.

   The PSU powers **only** the Pico. Two consequences. There is no second device on the
   supply to act as a witness, so a whole-supply dip cannot be confirmed by observing
   something else fail. And the largest load that adapter ever sees is the Pico's own WiFi
   TX bursts (250-300 mA peaks): a marginal adapter can dip on those alone. A mains sag
   coinciding with a TX burst is a compound trigger that would be rare, unreproducible on
   demand, and still correlated with switching.

10. 🔬 **Common-mode coupling across the optocoupler: hypothesis, speculative.**
    The doorbell is a **Ritto TwinBus**, roughly 24 V, fed from a PSU elsewhere in the
    building: a different circuit from both the Pico and the switched lights. An earlier
    version of this item assumed a shared circuit and is superseded.

    The separate PSU creates two independent ground references bridged by a single component:
    the Pico sits on the flat's mains via its USB adapter, the TwinBus sits on the building's
    PSU, and the optocoupler is the only thing spanning them. A step in the potential
    difference between those domains appears across the isolation barrier. Isolation blocks
    DC, but the barrier's inter-electrode capacitance (order 1 pF) passes displacement
    current on a fast dV/dt. Switching a load in the flat shifts the local reference relative
    to the building's.

    Consistent with observations for the same reason as the superseded version: the LED side
    needs real forward current for a real duration, while displacement current arrives on the
    output side by a different path. Resets without phantom rings is what it predicts.

    **New testable prediction.** TwinBus is a building-wide bus carrying other residents'
    calls and door-opener actuations. If this mechanism is real, reboots should correlate
    with **neighbours' doorbell activity**, not only with the flat's lights. Checkable once
    C4 is on production, using Telegram's own message timestamps.

    **The manufacturer acknowledges this failure class.** The TwinBus system handbook carries
    an explicit warning that devices with strong magnetic fields: contactors, transformers -
    must not be installed near the power supply or auxiliary units, because induced voltage
    spikes cause malfunctions. Ritto is documenting susceptibility to precisely the kind of
    transient under discussion here.

    **Installation rule worth checking on site.** The handbook requires mains and TwinBus
    wiring to be routed separately to satisfy VDE 0800: 10 cm apart, or with a divider where
    they share a conduit. Older buildings frequently do not respect this. If the separation
    is absent anywhere along the run, the coupling path becomes materially more plausible.

    **Prior art warning.** `tuxuser/ritto_doorbell`, an ESP8266 integration with the same
    TwinBus system, is archived with a note that it never worked reliably. The kind of
    unreliability is unstated, so this is suggestive rather than diagnostic, but it is a
    second known instance of a TwinBus tap misbehaving.

    *(An earlier note here questioned whether "pin 04" meant that project's GPIO04, which is
    a mute relay output. It refers to a terminal on the Ritto mainboard itself, per their
    TwinBus schematic. Retracted.)*

    Mitigations if confirmed, cheapest first: shorten the optocoupler-to-Pico wiring, add an
    RC on the optocoupler output, fit a ferrite on the doorbell pair, separate the two
    harnesses.

    *Note on sources:* `deh0511.de/twinbus`, the origin of the pinout diagram both prior
    projects cite, is a frameset and could not be retrieved programmatically. The pin table
    would need pasting in by hand.

    C4 adjudicates regardless: `chip=RUN` keeps it alive, `chip=POR/BOD` points at the
    supply.