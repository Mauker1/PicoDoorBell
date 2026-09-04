# PicoDoorBell — Refactor Roadmap

Baseline for this document is `main.py` and `secrets.py` as published on `main`.

Target: the bot must work identically whether it notifies a **DM**, a **group**, a
**supergroup**, or a **channel**.

---

## Guiding principle

The current firmware is a **silent-failure appliance**. When it breaks — WiFi wedged,
socket leak, unhandled `KeyError`, chat migrated — nothing tells you. You find out when
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
| ✅ | Landed and tested |
| 🔬 | Landed, awaiting hardware verification |
| — | Not started |

### 🚀 Promoted to production

The production unit is running the C5 build: `Doorbell input ready on GP16`, startup message
delivered to the production group, `state.json` written at the 60 s gate. MAC
`d8:3a:dd:af:cc:82`, distinct from the bench unit, so the two are not confusable.

**Discard `boot #1` from the dataset.** It reported `verdict=power` on a Thonny soft reboot,
which is the documented first-run ambiguity: the magic word had never been written, so `cold`
was inevitable, and `CHIP_RESET` still held `POR/BOD` from the last time the board was
powered. Affects only the first boot after a flash. **The reboot dataset starts at boot #2.**

✅ **Both remaining steps done.** A real ring delivered in about 2 s, matching the bench, and
the RUN cable is reconnected.

✅ **Positive control established.** Re-plugging the reset connector produced
`verdict=run-pin chip=RUN raw=0x00010000` on the production board. The register map works on
this unit, not just the bench one, and the instrument is now calibrated against a known
input: **if the spontaneous reboots come through the RUN line, they will say so.**

This supersedes the elimination approach entirely. The earlier plan — disconnect the cable,
wait, interpret silence — was weak precisely because no fault could be demonstrated in the
known-bad configuration. Detection replaces it: one labelled event is now enough.

The re-plug itself remains a calibration event, not a data point. Connector travel is
mechanical and does not occur in service (queue item 7).

Note that production runs the **C5 build, not B1** — it still polls the input with the 5 s
blocking sleep. B1 goes through the same bench-then-promote cycle. The reboot data is
unaffected either way.

### Bench validation complete — cleared for production

Verified end to end on the bench unit at v1.23.0: boot ordering, reset diagnosis across all
four paths, the per-revision board file, the request layer, the flash write and its stability
gate, the schema, and a doorbell press delivering to Telegram.

Promotion steps:

1. Back up production's current `main.py` on the device under another name.
2. Copy `boards/board_v1_2.py` to the device as `board.py` (GP16). Without it the firmware
   refuses to start — intended, but not mid-install.
3. Leave production's `secrets.py` alone.
4. Flash `main.py`.
5. Verify `Doorbell input ready on GP16`, a startup message in the production group, and
   `state.json` appearing after 60 s with `boots: 1`.
6. Test a real ring.
7. **Reconnect the RUN cable.** With C4 deployed, direct measurement beats elimination.

Reading the reboot data:

| Verdict | Meaning | Next step |
| --- | --- | --- |
| `power` | Supply — brownout or dip | Swap the USB adapter (queue item 9) |
| `run-pin` | RUN line | Bisect: lift the on-board button |
| `warm-reset` | Watchdog or soft reset | Unexpected before B2 exists |
| `unstable=N` | Boot loop, N attempts since the last stable start | Investigate before it recurs |

Known cosmetic issues, not faults: the "first time" wording above the boot counter (C6), and
spurious reconnect messages if anyone posts in the group, since `/log` parsing is
`channel_post`-only until A1.

**Phase 1 progress:** ✅ D1 · ✅ A5 · ✅ A3 · ✅ I2 · ✅ B3 · ✅ C4 · ✅ C5 · 🔬 B1 · — B2 · — B6 · — C3

Everything marked ✅ has host-side test coverage but has **not yet run on real
hardware**. See [Open questions](#open-questions) for what that gates.

---

Categories group work by *similarity*; priorities sequence it. Work the priorities
across categories, not the categories top to bottom. See
[Sequencing](#sequencing) for the concrete order.

---

## A. Bug fixes

### A1 — Universal update parsing — **P1**
Replace `result['channel_post']` with a resolver walking the known update keys in order:
`message`, `edited_message`, `channel_post`, `edited_channel_post`. Return `None` for
anything else (`my_chat_member`, `callback_query`, service updates) rather than raising.

Extract `chat.id`, `chat.type` and `text` defensively — a post with no `text` key (photo,
sticker, join event) must be skipped, not crash.

This is the change that makes DM / group / supergroup / channel work from one code path.

> **Why it matters:** `channel_post` is emitted *only* for channels. In a group, every
> single update currently raises `KeyError`, which propagates to the catch-all handler and
> triggers a spurious `wlan.disconnect()`.

### A2 — Command matching that survives group conventions — **P1**
Parse as `text.strip().split()[0].split('@')[0]`, compared case-insensitively, so `/log`,
`/log@PicoDoorBellBot` and `/log extra args` all match. In groups Telegram's client
appends the bot username via autocomplete, and does so mandatorily when multiple bots are
present. Add a small dispatch table so future commands don't mean touching the parser.

### ✅ A3 — HTTP status checking — **P0**
`send_message` and `read_message` must inspect `response.status_code`.

`urequests` **does not raise on 4xx** — it returns a response object with the error status.
Today every API failure is invisible: the code prints "Doorbell pressed!" and carries on
believing it delivered.

Classify:

- `2xx` — success
- `429` — read `retry_after` from the body, back off
- other `4xx` — permanent, log and give up
- `5xx` / `OSError` — transient, retry with backoff

Prerequisite for A4 and A6.

### A4 — Chat migration handling — **P0**
On a 400 response, check the body for `parameters.migrate_to_chat_id`. If present, persist
the new ID (Tier 2, see [I1](#i1--tiered-state-model--p1)) and retry.

> **Why it matters:** a group becomes a supergroup automatically when made public, when it
> exceeds the member threshold, and in some cases on admin assignment. The chat ID format
> changes from `-123456789` to `-100123456789` and the old ID stops working. Without this,
> notifications stop permanently and silently.

Depends on A3, I2.

### ✅ A5 — Guaranteed response cleanup — **P0**
Every request in `try/finally` with `response.close()` in the `finally`. Follow each request
with `gc.collect()`.

Currently `response.close()` sits after the `for` loop in `read_message`, outside any
guard — any exception during parsing leaks the socket. This is the classic MicroPython +
`urequests` slow death: works for days, then `ENOMEM`. The existing `MemoryError` recovery
path (disconnect WiFi, sleep 10 s) frees nothing.

### A6 — Log lifecycle correctness — **P1**
Three defects, one fix:

1. Clear the log after a **confirmed successful send**, not only on overflow. Today
   `print_log` resets only when `len(log) > logMaxSize`, so `/log` against a small log
   leaves it untouched — contradicting the README.
2. Chunk output to Telegram's **4096-character** limit. `logMaxSize` is 10000, so a full
   log is an unconditional 400.
3. **Never discard a log that failed to send.** Today the 400 is invisible (A3), control
   returns, the overflow condition is true, and `reset_log()` wipes it — you ask for the
   log, nothing arrives, and the log is destroyed.

Depends on A3.

### A7 — Boot-time backlog flush — **P1**
Call `getUpdates` with `offset=-1` once at startup to discard the buffered backlog.
`updateId` starts at 0, so the first poll returns up to 100 buffered updates and a stale
`/log` from hours ago replays on every reboot.

### A8 — Query string cleanup — **P3**
`?offset=X` then `&`, and drop the `chat_id` parameter entirely — `getUpdates` does not
accept it and Telegram silently ignores it.

> **Note:** this works today. A `?` is legal inside a query component, so the parameter
> parses as key `offset` with value `123?chat_id=456`, and Telegram reads the leading
> digits and stops at the first non-digit. It is not a bug — it is reliance on undocumented
> parser leniency. Cheap to fix, low priority.

---

## B. Reliability

### ✅ B1 — IRQ-driven doorbell input — **P0, awaiting hardware test**
**The headline fix.** `Pin.irq(trigger=IRQ_RISING)` sets a latch flag; the main loop only
drains it.

Today the input is polled once per second, but the same loop performs a blocking `getUpdates`
every 60 s (TLS handshake on a Pico W is easily 1–3 s) and sleeps 5 s after a detected press.
During all of that the pin is not observed at all. **A short bell pulse landing in one of
those windows is lost silently.**

Requirements:

- ISR does nothing but set a flag and return — no allocation, no I/O
- Timestamp-based debounce (ignore edges within N ms)
- Must tolerate the CPU stalling during a flash erase (see [Flash](#appendix--flash-wear-analysis))

**Design parameters, now that G7 is measured:**

- **Trigger:** `IRQ_RISING` on a 0 → 5 V transition.
- **Debounce and lockout are separate timers**, which the current 5 s sleep conflates.
  Debounce is tens of milliseconds, to reject edge noise. Lockout must exceed the 2 s pulse
  so one ring yields one notification; the existing 5 s is a sensible value.
- **Validate the pulse width before notifying.** Require the input to stay high for
  100–200 ms before accepting a ring. Against a 2 s signal this costs nothing, and it
  rejects short transients outright.

**Instrument the misses.** Count presses that the IRQ latches *while a blocking operation is
in progress* — those are exactly the rings the old polling loop would have dropped. Report
the count in the heartbeat (C3).

> A missed notification is unobservable by construction: the physical chime still sounds, so
> a dropped alert only matters when nobody is home to hear about it. Neither firmware version
> has ever shown a missed ring, which usefully bounds the rate from above but cannot
> distinguish zero from a handful per year. The counter replaces the estimate with a
> measurement, and costs nothing — the latch already has the timestamp and the loop already
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
> input** — G9 is deferred and unproven, and building for a hypothetical second input would
> be over-engineering. The point is only to avoid a structure that would later have to be
> undone.

### B2 — Watchdog — **P0**
`machine.WDT`, fed from the main loop. Timeout must accommodate **both** a slow TLS
handshake (~8 s) **and** a worst-case flash sector erase.

> ✅ **Measured: 1–2 s per Telegram round trip** on the bench unit, detection to delivery.
> Healthy on its own, but the RP2040 watchdog ceiling is roughly **8.3 s** and the current
> loop can exceed that in one iteration:
>
> | Step | Worst case |
> | --- | --- |
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

### ✅ B3 — Fully guarded boot path — **P0**
Configure the GPIO and IRQ **before** touching the network, and wrap the entire startup in
exception handling.

Today `connect_wifi()` runs at module scope outside the `try`, and sends `startupText`
immediately after association — precisely when DNS is least likely to be ready. If that
first send throws, the script dies with a traceback and `doorBellInput` is **never
created**. With no watchdog, the device is dead until someone power-cycles it.

Also: remove the message-sending side effect from inside `connect_wifi()`. Connection and
notification are separate concerns.

### B4 — Bounded WiFi reconnection with backoff — **P1**
> **Mark the reset as intentional.** The `machine.reset()` after N failed cycles would
> otherwise be indistinguishable from a watchdog bite — both read as `warm-reset`. Set a
> marker in a spare scratch register before resetting, and clear it once read, so
> self-inflicted resets never pollute the B2 watchdog data.

Interpret `wlan.status()` properly (`-3` wrong password, `-2` no AP found, `-1` link fail,
`3` connected). Exponential backoff instead of the current fixed 3 s hammer. After N failed
cycles, `machine.reset()`.

Distinguish permanent misconfiguration (bad password — retrying forever is pointless, blink
an error code) from a transient outage.

### B5 — Proportionate error recovery — **P1**
Stop calling `wlan.disconnect()` on every exception. One failed POST currently tears down
the network, which triggers a reconnect and an *"I am back online!"* message — turning a
flaky moment into notification noise.

Classify: network errors → retry; parse errors → log and continue; `MemoryError` →
`gc.collect()`, then reset if it recurs.

### B6 — Offline event queue — **P0**
Latch doorbell events with timestamps into a bounded **RAM** queue and flush on reconnect.
Messages delivered late must be marked as delayed.

Today a press during a WiFi outage is simply lost — the one thing a doorbell notifier must
never do.

**RAM-first by design.** The queue exists to survive a *WiFi* outage, which RAM handles
completely. Flash only helps in the narrow case of a press followed by a reset before
delivery. Snapshot to flash (Tier 3) only on a rare trigger — queue non-empty *and* outage
exceeding a threshold, or immediately before a deliberate `machine.reset()`. This cuts write
count by roughly two orders of magnitude versus persisting every event, at almost no cost in
real-world reliability.

### B7 — Alert rate limiting — **P2**
Cap alerts per rolling window. A stuck-high input line currently means one message every
5 seconds, forever. On suppression, send **one** "input appears stuck" notice rather than
falling silent.

### B8 — WiFi power save — **P2**
`wlan.config(pm=0xa11140)` for the mains-powered build; the well-known Pico W latency fix.

Make it a **config flag**, not a hardcoded call — the battery build will want the opposite
tradeoff.

---

## C. Observability

### C1 — Real timestamps — **P1**
NTP sync at boot with periodic resync. Fall back to `ticks_ms` when unsynced and mark those
entries as such.

`ticks_ms` wraps at ~12.4 days, and `3847221` is useless in an incident report.

### C2 — Structured, bounded logging — **P2**
Levels (DEBUG / INFO / WARN / ERROR). Ring buffer that drops oldest rather than refusing new
entries — the current `append_to_log` stops accepting anything once full, so the log
preserves the *least* recent events. Include `gc.mem_free()` and uptime on every entry so a
leak is visible as it develops.

Also remove the debug noise: `print(response.text)` materialises a full `str` copy alongside
the cached `bytes` body on a 264 KB part, and the exception handler dumps the entire ~10 KB
log to console on *every* error.

**Explicitly Tier 0 — RAM only.** See [I1](#i1--tiered-state-model--p1). Crash survival is
handled by a compact code in the scratch registers plus, optionally, a *single* flash write
on the way to a reset. Never continuous log persistence.

### ✅ C5 — Persist the boot counter to Tier 2 — **P1**
The Tier 1 boot counter counts almost nothing useful. Bench testing showed any hardware
reset clears the scratch area — a power cycle and a RUN press both report `boot #1`. It
therefore only counts soft reboots and watchdog bites, and contributes nothing to the
production reboot dataset, which is entirely about power and RUN events.

C4's *cause* half works and is the more important half. The *count* half does not.

**Fix:** move the counter to Tier 2 (`state.json`), keeping the magic word in Tier 1 for
warm/cold detection. One write per boot is affordable — daily reboots are ~365 writes a year
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
number means — it becomes a count of *stable* boots, and any divergence from reality is
itself a signal.

Pair with a Tier 1 **unstable-boot counter**: incremented at boot, cleared when the 60
seconds elapse. Free, no flash, and it makes a boot loop visible in the next heartbeat
instead of invisible.

### C6 — Fix the startup message wording — **P3, trivial**
`startupText` reads "I am online for the first time! Bot started!" but `isStartup` is a RAM
flag, so it is `True` on every boot — every restart claims to be the first.

Harmless until C5, which now prints a boot counter directly beneath it. "For the first time"
above `boot #47` is self-contradictory.

The flag's actual job is distinguishing the boot-time announcement from a post-reconnect one
*within a session*, which the wording does not reflect. Reword to "Bot started." and
"Reconnected.", and let the reset line carry the boot number.

Fold into whichever commit next touches those strings, or into E1 when the message text moves
to configuration.

### C3 — Heartbeat — **P0**
Periodic "alive" ping carrying uptime, free memory, RSSI, alert count and flash write count.
Optionally a `/status` command for on-demand health.

This is the single change that converts silent failure into visible failure. Rank it
alongside B1 in value.

Keep the payload schema **extensible** so battery voltage can be added later without a
breaking change.

### ✅ C4 — Reset cause and boot counter (Tier 1) — **P1, hardware-verified**
Read `machine.reset_cause()` at boot, keep a boot counter in a watchdog scratch
register, log both, and include them in the startup message and heartbeat.

> **Must land before B2.** The production unit reboots intermittently — correlated with
> switching mains loads on the same circuit — and the cause is unknown. Today every restart
> looks identical: a startup message in Telegram. Once the watchdog exists, a wedged
> network stack produces a reset indistinguishable from that mystery unless the cause is
> recorded first.

Distinguishes:

| Cause | Meaning |
| --- | --- |
| `PWRON_RESET` | Supply dropped — brownout or mains dip |
| `HARD_RESET` | RUN pin driven low — button, or transient pickup on the trace |
| `WDT_RESET` | Firmware wedged (once B2 exists) |
| `DEEPSLEEP_RESET` | Future battery build only |

Roughly 20 lines. Converts an ongoing mystery into a dataset, and its value grows with
uptime — so it wants to reach the production device early.

---

## D. Security

### ✅ D1 — Stop leaking the bot token — **P0, trivial**
Remove `print(url)` from `read_message`, or redact the token.

It currently prints `https://api.telegram.org/bot<TOKEN>/getUpdates?...` on **every poll**.
Anyone pasting Thonny output into a GitHub issue or forum thread hands over full control of
their bot.

### D2 — Secrets file hygiene — **P1**
Ship `secrets.example.py`, gitignore `secrets.py`, remove the tracked file from the docs
flow. Add a startup check that refuses to run on unreplaced placeholders with a clear error.

The README currently instructs users to edit a **tracked** file containing their WiFi
password and bot token.

Also add a `deviceName` field. With a bench unit and a production unit running
simultaneously against separate bots and groups, every startup message, heartbeat and log
line needs to say which board sent it — and crossing the two `secrets.py` files either
floods the real group or sends production alerts to a test channel nobody watches.

### D3 — Sender authorization — **P1**
Verify incoming updates originate from the configured chat before acting on commands.
Optionally maintain an allowlist of user IDs for command issuance.

In a group, **any member** can currently request the log, which contains the local IP and
network history.

Related: confirm the BotFather privacy-mode setting (`/setprivacy`). With privacy **on**,
the bot only receives commands and replies. With it **off**, it receives every message,
photo, sticker and join notification in the group — each one currently an unhandled
exception plus a WiFi teardown.

### D4 — TLS posture — **P3, research**
`urequests` does not verify certificates, so the bot token is exposed to a MITM on the local
path. Full verification is awkward within the Pico W RAM budget.

Minimum deliverable: document the exposure honestly in the README. Investigate whether
current MicroPython builds make pinning to Telegram's CA feasible. Treat as research, not a
committed deliverable.

---

## E. Architecture

### E1 — Configuration extraction — **P1**
> **Pin assignments already extracted.** The two boards use different doorbell input pins
> (GP16 vs GP18), so `doorBellPin` moved to a per-revision `board.py` ahead of schedule. The
> rest of E1 — intervals, message strings, country code, polarity, feature toggles — still
> belongs to Phase 2, and should go somewhere other than `board.py`, which is deliberately
> limited to hardware wiring.

`config.py` holding: GPIO pin, country code, all intervals, all message strings,
active-high/active-low polarity, feature toggles.

The README's "Final details" section is currently a list of *"now go edit source code"*
instructions. It should become *"edit `config.py`."*

### E2 — Module split — **P2**
Roughly: `config.py`, `wifi.py`, `telegram.py`, `applog.py`, `doorbell.py`, and a thin
`main.py`.

> **Caution:** the current `while True` loop runs at module scope, so assignments like
> `lastLogCheck = ...` mutate globals implicitly. Every one of these becomes a genuine bug
> the moment the loop is wrapped in a function. This refactor must be done deliberately,
> not mechanically — and **after** F1.

### E6 — Do not offload networking to core 1 — **decision record**
The RP2040 has two Cortex-M0+ cores and MicroPython exposes core 1 via `_thread`. Moving the
network work there is a natural idea. **Rejected.**

**It solves a problem B1 already solves, and solves it worse.** The reason to offload
networking is to stop it blocking doorbell detection. A pin IRQ does that in about ten lines,
and a hardware interrupt preempts everything — including a running thread — so the input is
captured regardless of what either core is doing.

It buys nothing else. Notification latency is dominated by the TLS handshake, which is no
faster on core 1. There is no other work for core 0 to do meanwhile, and the device handles
one event type.

Supporting reasons:

- `_thread` on rp2 is documented as experimental, and running the network stack on core 1 is
  a known trouble spot — lwIP and the cyw43 driver are not thread-safe in MicroPython. The
  v1.29.0 release notes list thread fixes for the rp2 port, so the area has been actively
  broken.
- MicroPython uses a GIL, so two threads interleave rather than running in parallel.
  Concurrency, not parallelism — considerably less than the hardware implies.
- **Watchdog interaction, the one that would actually bite.** If core 1 wedges while core 0
  keeps feeding the watchdog (B2), the device never resets and the fault is invisible.
  Avoiding that needs a cross-core liveness check — more machinery than the thing being
  built.
- Core 1's stack comes from the same 264 KB heap already shared with TLS buffers.

**Where it would be legitimate:** genuine concurrent work, such as audio streaming to a SIP
extension. Not for stopping one HTTPS POST from blocking a pin read.

### E3 — Notifier abstraction — **P3**
A minimal `send(event)` interface so Telegram becomes one backend among several. Enables E4
without further surgery.

### E4 — MQTT / Home Assistant backend — **P4**
Dramatically lighter and lower-latency than TLS HTTP on this chip, and what the
home-automation audience actually wants. With HA discovery the doorbell becomes a
first-class entity.

### E5 — Long polling — **P3**
`getUpdates` with a `timeout` parameter reduces request volume — but it **blocks**, which is
only safe once B1 has decoupled input capture from the loop. **Strictly ordered after B1.**

---

## F. Testing & tooling

### F1 — Host-side stubs — **P2**
Fake `machine`, `network`, `rp2`, `urequests` modules so the logic runs under CPython.

This is the enabler for the whole category, and it is what makes E2 safe to attempt.

### F2 — Unit tests — **P2**
Cover:

- the update parser, against **real captured payloads** for all four chat types
- junk cases: no `text` key, service updates, `my_chat_member`
- the migration response shape
- the command parser (`/log`, `/log@bot`, `/log args`, case variants)
- the log chunker at the 4096 boundary
- the connection state machine

### F3 — CI — **P3**
Ruff lint, run tests, `mpy-cross` compile check to catch syntax errors before they reach a
board.

### F4 — Style pass — **P3**
PEP 8 naming, drop parenthesized conditions, remove dead code (`startupTime`, trailing
`pass`, unused loop variable), fix the `chatId` parameter shadowing the module global.

**Do this last, as a single commit,** so it doesn't obscure the substantive diffs.

---

## G. Hardware & UX

### G1 — LED state machine — **P2**
Distinct patterns for connecting / connected / error / alert-sent.

Fix the inversion: the error handler calls `wlan.disconnect()` and then `led.on()`, leaving
the "connected" indicator lit on a disconnected device. It is the only local feedback there
is.

### G2 — Battery telemetry — **P4**
VSYS via ADC3, with the GPIO25-high sequencing the Pico W requires. Report in the heartbeat,
warn on low.

Deferred: no hardware to read on V1.1.

### ✅ G7 — Characterise the ring pulse — **P1, was blocking B1**
Measure how long the optocoupler output actually stays asserted during a ring.

✅ **Measured.** Terminal 04 idles at ground and goes **high to 5 V for approximately two
seconds** on a ring: a clean digital square wave, not an edge and not a pulse train.

Consequences:

- **Ring loss is rare, not routine.** A 2 s assertion against a 1 s poll is normally caught.
  A ring is only missed if a blocking call spans the whole pulse — a ~3 s TLS handshake once
  a minute gives roughly a 1–2% miss rate per ring. A handful per year, not the systematic
  loss feared earlier. B1 remains worth doing; it is not an emergency.
- **Polarity already correct.** Idle low, active high → `IRQ_RISING`, matching the existing
  `PULL_DOWN` and `pressed = 1`.
- **Both buttons assert it** (see G9), so the 2 s level says "someone rang", not which door.

Original reasoning, retained for context:

**Semantics confirmed, duration still unknown.** The deh0511 pinout documents terminal 04
as a bell-signal output at roughly 5 V DC, matching Bracke's description of it as an
ordinary button press. It is a level, not a brief edge — better for the current polling loop
than the `tuxuser` project's "ring **pulse**" wording implied.

What remains unmeasured is **how long it stays asserted**, which is the number that decides
whether a one-second poll, a five-second post-press sleep, and multi-second blocking TLS
calls can miss it.

> **Measure under load, not open-circuit.** A signalling output is not necessarily a stiff
> supply. 5 V through the 180 Ω series resistor draws roughly 21 mA into the PC817 — a
> healthy LED drive, but a substantial load for a signal pin. If the level sags under it,
> that is an argument for raising the resistor.

> **A missed ring is silent.** No error, no log entry — just someone at the door who leaves.
> Level semantics make this less likely than a pulse would, but not impossible.

Two ways to measure, either is fine:

- Scope or logic analyser on the optocoupler output during a real ring.
- A tight-loop MicroPython script printing the pulse width in milliseconds. No equipment
  needed; five minutes on the bench with a jumper standing in for the bell.

**Sets B1's debounce window**, so it wants doing before B1 is written. Also feeds G3.

✅ **Both buttons assert terminal 04** — confirmed by testing. See G9 for the consequence.

While measuring, also capture **whether terminal 04 and the ED line assert simultaneously or
with an offset**. That sets the correlation window G9 needs.

### G3 — Input signal conditioning — **P2**
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

### G8 — Do not power the Pico from the bus — **P3, decision record**
Terminal 05 on the 7630 carries the +24 V bus supply. It is tempting: if the reboots turn
out to be supply-related, powering from the bus would remove the USB adapter from the
picture entirely.

**Recorded as rejected, and now settled by the numbers.** The TwinBus system handbook gives
the 17573 power supply's output to the system bus as **15 V DC at 200 mA**. A Pico W's WiFi
TX bursts alone are 250–300 mA — the transmit peak exceeds the entire system's DC budget.

Bracke tried precisely this on the same system, a DC-DC converter off the bus, and reported
that the intercom stopped working for lack of power; he moved to a separate supply. That is
exactly what these figures predict. In a multi-party building the margin belongs to
everyone's doorbell, not just this one.

Written down so it does not resurface as an obvious idea.

### G9 — Distinguish building entrance from flat door — **P3, deferred**
Terminal 04 asserts for **both** the building door station and the flat's own Etagendrücker,
confirmed by testing. Notifications therefore conflate two different events: someone at the
building entrance, versus someone already inside at the flat door — usually a neighbour or a
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

### G6 — RUN pin noise immunity — **P1**
Fit **100 nF from RUN to GND**, as close to the pin as layout allows, plus a **10 kΩ
pull-up from RUN to 3V3**.

The RP2040's internal RUN pull-up is weak (~50 kΩ), leaving a high-impedance node with a
length of wire attached and little noise immunity.

> **Note on evidence.** This item was originally justified by the observation that re-mating
> the reset connector rebooted the board. That observation has since been set aside as an
> artifact of connector travel — see hardware queue item 6 — and is *not* evidence for
> electromagnetic pickup. G6 remains worth doing as standard practice for a RUN line with
> wire attached, but it is **precautionary**: nothing currently confirms RUN as the cause,
> and C4 should decide whether it justifies a board revision.

The capacitor gives roughly a 5 ms time constant against the internal pull-up: long enough
to swallow a transient, short enough not to interfere with the button or startup. The
external pull-up lowers the node impedance about 5×, so a given injected charge moves the
voltage far less. Together they are meaningfully better than either alone.

Prototype on protoboard first, then a carrier board revision. Also check whether the reset
cable runs near the doorbell wiring or mains — rerouting may help as much as the capacitor.

> This establishes that the RUN line *can* be disturbed trivially. It does not yet prove
> the mains-switching reboots share that mechanism: plugging a connector is a physical
> disturbance, a light switch is an electromagnetic one. C4 closes that gap — `chip=RUN` on
> a reboot coinciding with a light switch is the confirmation.

### G4 — Pico 2 W support — **P3**
Verify and document. It is the board most people would buy today.

### G5 — Preserve the deep-sleep path — **P3, architectural constraint**
A future battery build will want `lightsleep`/`deepsleep` with IRQ wake, which is
incompatible with a busy main loop that assumes it runs forever.

Nothing to build now — but E2's module split must keep the event loop **swappable** rather
than baking `while True: sleep(1)` into the architecture. Cheap to honour now, expensive to
retrofit.

> On battery, unexpected power loss goes from rare to routine, which raises the value of
> B6's flash snapshot. Another reason to build the mechanism now even if it triggers rarely.

---

## H. Documentation

### H5 — Ritto/TwinBus safety warning — **P2**
The README treats this as a standalone doorbell project. It is not: the TwinBus is a
building-wide system fed from a PSU in a shared area. Bracke's writeup warns that mistakes
connecting to a Ritto installation can damage the whole building's system, not just the
endpoint.

That is a materially different risk profile from cutting into a private doorbell, and the
README should say so before the wiring instructions. The same caution appears
community-sourced: a mikrocontroller.net poster with a logic analyser held off from probing
his own installation because it served four parties and a mistake would have taken out
everyone's doorbell.

Terminal conventions worth documenting alongside it, since they recur across Ritto
manuals: `a`/`b` are the two bus wires, `ED` is *Etagendrücker* (the flat's own door
button), `TÖ` is *Türöffner* (door opener).

The board-level points on the 7630 Wohntelefon are reverse-engineered rather than official.
Per the deh0511 pinout, the ones this project touches are **04** (bell signal output, about
5 V DC) and **03**/**09** (ground). **05** carries +24 V bus voltage — see G8 for why it
should be left alone. Several points on that connector remain undocumented. Credit
`deh0511.de/twinbus` as the source rather than reproducing the table wholesale. Also worth linking the prior art
(`deh0511.de/twinbus`, beechy.de, `tuxuser/ritto_doorbell`) as pinout references.

### H1 — Broken links and typos — **P1, trivial**
- Malformed SMD gerber URL: `.../blob/main/(assets/Gerber_...zip)` — parenthesis inside the
  URL, 404s
- Both gerber links reference the same filename; unclear which is which
- "EF2 file" → **UF2**, two occurrences
- Parts list says **180 Ω**; "Final details" says **185 Ω equivalent**
- Parts list "5.1 ohms" — verify; 5.1 kΩ seems far more plausible
- Heading reads "V 1.2 schematics" but the image is `schematicsV01_1.png`

### H2 — Setup rewrite — **P2**
Document the DM, group, supergroup and channel paths properly now that all four are
supported. Include the supergroup migration caveat and the BotFather privacy-mode setting.

### ✅ H4 — Architecture document — **P2**
`docs/ARCHITECTURE.md` describes how the firmware works and why: the state tier model, the
boot sequence, the HTTP request layer, the on-device file layout. Kept separate from the
README (a build guide) and from this file (a plan). Updated alongside each commit that
changes behaviour it describes.

### H3 — Repo hygiene — **P3**
Tagged releases matched to PCB revisions, a CHANGELOG, and a troubleshooting section built
from the failure modes catalogued here.

---

## I. Persistence & state

### I1 — Tiered state model — **P1**
Split persisted data by write frequency, and **enforce the tier in code**, not by convention.

| Tier | Medium | Contents | Write frequency |
| --- | --- | --- | --- |
| 0 | RAM | Rolling log, alert counters, live queue | Never persisted |
| 1 | Watchdog scratch registers | Reset reason, boot count, crash code | Free, zero wear |
| 2 | Flash (`state.json`) | Migrated chat ID, NTP epoch anchor, config overrides | Transition-driven; a handful per device lifetime |
| 3 | Flash (snapshot) | Queue contents | Rare, conditional only (see B6) |

**Tier 1 detail:** the RP2040 has eight 32-bit watchdog scratch registers that survive both
a soft reset and a watchdog reset (not power loss) — 32 bytes of free, zero-wear,
reset-surviving storage, reachable via `machine.mem32` at the watchdog base.

> ✅ **Verified on hardware.** Scratch 2 and 3 behave as expected on v1.23.0. Note that any
> hardware reset clears them, RUN included — not only power loss, as first assumed.

**The one rule that matters: never write flash on a timer.** Everything else follows.

### ✅ I2 — Atomic write helper — **P1**
One function, one file, one write. Serialize all Tier 2 state together, write to
`state.tmp`, `os.rename()` over the target. Rename is atomic under littlefs, so a power cut
mid-write cannot leave corrupt state.

Read-before-write comparison so an unchanged value never triggers an erase.

Costs one extra erase per write — irrelevant at these frequencies, and it buys integrity.

**Do not rewrite `secrets.py`.** Runtime state belongs in a separate file; rewriting a source
file the user also edits by hand invites a merge conflict on a microcontroller.

### I3 — Wear instrumentation — **P2**
Keep a monotonic write counter inside the state file and report it in the heartbeat (C3).

This makes wear **observable** rather than theoretical. If the counter climbs faster than
expected, there is a bug in the write discipline — and it surfaces in month one instead of
year three.

### ✅ I4 — Filesystem verification — **P3**
Confirm whether the build uses littlefs2 or FAT, and record it in the troubleshooting
section. It changes the endurance envelope by an order of magnitude.

---

## J. Deployment & updates

### J1 — Over-the-air updates — **P3**
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

### K1 — Adopt `machine.mem_backup()` for Tier 1 — **P3**
C4 writes the boot counter and magic word into watchdog scratch registers via raw
`machine.mem32`, using a register map taken from the datasheet and not yet confirmed on
hardware. MicroPython v1.29.0 adds `machine.mem_backup()`, a supported API for
hard-reset-surviving memory on rp2 and six other ports, returning a writable memoryview.

Adopting it would remove our dependence on an unverified register map. Two constraints:

- **Version gating.** The project targets other people's boards, many on older builds, so
  this has to be runtime-detected — use `mem_backup()` when present, fall back to `mem32`
  otherwise — rather than a hard requirement.
- **Possible conflict.** If `mem_backup()` is backed by the scratch registers C4 already
  uses, the two will collide on any build that has it. Verify before upgrading either board
  past v1.28.

Blocked on the firmware upgrade, which is itself blocked on identifying the reboot cause.

### K2 — Track rp2 port improvements — **P4**
v1.29.0 brings roughly a 10% rp2 performance gain and fixes for threads, lightsleep and
UART IRQ latency. The lightsleep fixes are relevant to G5 and the future battery build.
Nothing to do until the upgrade happens; recorded so it is not rediscovered later.

---

## Sequencing

### Phase 1 — Stop the bleeding
✅ `D1` → ✅ `A5` → ✅ `A3` → ✅ `I2` → ✅ `B3` → ✅ `C4` → ✅ `C5` → 🔬 `B1` → `B2` → `B6` → `C3`

**Hardware checkpoint after B3**, before B1. Everything landed so far is host-tested only,
and B1 changes interrupt behaviour — the hardest thing to debug with unverified changes
underneath it. B1 and B2 additionally *cannot* be validated in stubs: debounce timing
against a real optocoupler and a watchdog timeout that must survive a real TLS handshake
both need the bench unit.

Small, surgical, no restructuring. After this the device stops losing presses and stops
failing silently. **The bulk of the early effort belongs here.**

`I2` lands ahead of `A4` because the migration fix needs somewhere safe to write, and the
atomic-write helper is roughly fifteen lines. Establish `I1`'s tier discipline at the same
time — before anyone adds a second `open(..., 'w')` somewhere convenient and quietly starts
writing on a timer.

`C4` lands ahead of `B2` so that watchdog resets stay distinguishable from the existing
unexplained reboots.

### Phase 2 — Correctness
`A1` · `A2` · `A4` · `A6` · `A7` · `B4` · `B5` · `C1` · `D2` · `D3` · `E1` · `I1` · `H1`

The device now behaves correctly across all chat types.

### Phase 3 — Restructure
`F1` → `E2` → `F2` → `F3`

**Tests before the refactor, not after.** F1 is what makes E2 safe.

### Phase 4 — Polish and roadmap
`B7` · `B8` · `C2` · `G1` · `G3` · `I3` · `E3` · `E5` · `A8` · `D4` · `F4` · `G4` · `G5` ·
`H2` · `H3`

Plus `J1`, once `C4` and `B2` are proven on hardware.

Then, on future hardware: `E4` · `G2`.

### Dependency edges worth respecting

```
A3 ──▶ A4 ──▶ (needs I2)
A3 ──▶ A6
I2 ──▶ A4, B6-snapshot, I3
B1 ──▶ E5
C4 ──▶ B2   (reset causes must be separable before adding a new one)
C4 ──▶ J1   (rollback needs to detect a failed boot)
I2 ──▶ J1   (atomic swap reuses the same pattern)
F1 ──▶ E2 ──▶ F2
B1 ◀──▶ G3   (debounce parameters are shared)
E2 ──▶ G5   (loop must stay swappable)
```

---

## Appendix — Flash wear analysis

The Pico W carries 2 MB of QSPI NOR (W25Q16-class): **4 KB erase sectors**, **~100,000
program/erase cycles per sector**, 20-year retention. MicroPython's firmware occupies
roughly 600–700 KB, leaving ~1.3 MB — about 330 sectors — for the filesystem.

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
dynamic wear leveling — allocating from a rotating pointer across free blocks rather than
rewriting in place. But verify the build first (I4); if it is FAT there is no leveling and
the table above is the real number. Note also that littlefs does dynamic but **not static**
wear leveling, so blocks holding rarely-changed data never rotate into the pool, and the
metadata pair stays comparatively hot. Design to the floor; treat leveling as headroom.

### Why the discipline matters even though a Pico W is cheap

Worn NOR flash **does not fail cleanly**. It begins failing *writes* while reads still
succeed. The result is a device that boots, runs, looks healthy, and silently stops
persisting state — the exact failure class this entire refactor exists to eliminate.

### Two constraints this imposes on the design

1. **Flash writes stall the CPU and disable interrupts.** A sector erase blocks for tens of
   milliseconds, worst case a few hundred. Therefore: B2's watchdog timeout needs margin for
   a worst-case erase, and **never write flash from inside an ISR**.
2. **A doorbell edge arriving during a write is not lost.** The RP2040 latches GPIO edge
   events in hardware, so the interrupt fires late rather than never — provided B1's ISR
   stays minimal (set a flag, return).

---

## Open questions

- **Filesystem type** on the target build — littlefs2 or FAT (I4). Changes the endurance
  envelope by 10×.
- **Scratch register availability** in the specific MicroPython build (I1, Tier 1). Confine
  to 0–3 and verify.
- **AC vs DC bell signal** (G3) — determines whether debounce is software pulse-coalescing
  or an RC stretcher on the opto output.
- **BotFather privacy mode** on *both* bots (D3) — must be set identically. A mismatch makes
  the bench unit hit the `channel_post` crash path constantly while production looks fine,
  or the reverse, and the difference looks like a firmware bug.

### Hardware verification queue

Open items for the bench unit, none yet answered:

1. ✅ **MicroPython version parity — done.** Both boards now run **v1.23.0** (2024-06-02).
   The prototype was brought up from v1.19.1 to match production, not the reverse:
   production is the instrument for the reboot dataset, and reflashing it would have reset
   an uncollected baseline while adding a second changed variable alongside C4.

   Side effect: flashing the `.uf2` erased the prototype's filesystem, so it starts with no
   `state.json`. That is a clean first-boot condition for testing I2 — expect
   `cause=PWRON_RESET`, `boot #1`, `cold`, and no state file until something writes one.

   This also **retires firmware age as a reboot hypothesis**. It was raised on the
   assumption production might be the older board. June 2024 is recent, and the early Pico W
   lwIP problems were fixed well before it. Remaining candidates: supply sag and RUN-pin
   pickup.

   The `ujson`/`uos` fallback stays regardless — v1.23.0 provides both names, but the
   project targets other people's boards too.

   **Not upgrading to the current release yet.** v1.29.0 (2026-08-24) is the latest, with
   v1.28.0 (2026-04-06) before it. Both are declined for now:

   - Production is the measuring instrument for an unresolved fault with no baseline and no
     confirmed cause. Changing the runtime adds a variable to an experiment that already has
     too many — if the reboots stop, the cause would be unattributable between firmware, the
     disconnected cable, and chance.
   - v1.29.0 is days old and a large release (a new port, a new `machine.mem_backup()` API
     across seven ports). A wall-mounted appliance wants a version with field exposure.

   **Sequence:** prototype to v1.23.0 now for parity → identify the reboot cause → then
   upgrade as a deliberate experiment, prototype first with a multi-day soak, production
   after. Prefer v1.28.0 or a v1.29.x point release over v1.29.0 at that time.

   ⚠️ **Check before any upgrade past v1.28:** v1.29.0 adds `machine.mem_backup()`, which
   exposes hard-reset-surviving memory on rp2. If it is backed by the same watchdog scratch
   registers C4 writes to, MicroPython may use or clear them. The magic-word check would
   catch it — every boot would report as cold — but that is a silent degradation, not an
   error. See K1.
2. ⚠️ **Prototype doorbell input pin — the boards differ.** Production uses **GP16**, the
   prototype uses **GP18**, confirmed from the firmware saved off the prototype before
   reflashing.

   **This blocks bench testing.** `doorBellPin = 16` is a constant in `main.py`, so flashing
   the current tree to the bench unit would leave the input on the wrong pin — and fail
   silently, since nothing would ever assert it.

   ✅ **Resolved.** Pin assignments now live in `board.py` on the device, copied from a
   per-revision file in `boards/`. `main.py` holds no pin defaults and refuses to start
   without a complete definition — an earlier proposal to put the pin in `secrets.py` was
   rejected, correctly, because a GPIO number is not a secret. See `ARCHITECTURE.md`.
3. ✅ **Filesystem type — littlefs2.** `os.statvfs('/')` returns 4096-byte blocks, 212
   total, 202 free. The block size matches the flash sector, which is characteristic of
   littlefs2 and consistent with the rp2 port's long-standing default. The atomic-rename
   guarantee in `ARCHITECTURE.md` holds. Strong inference rather than proof; the decisive
   test is case sensitivity, since littlefs distinguishes `AAA` from `aaa` and FAT does not.

3c. ✅ **Memory baseline — 178,480 bytes free** after boot with WiFi up. Reference point for
   the leak watch.

3b. ✅ **Ring pulse width — measured.** Terminal 04 idles at ground and goes high to 5 V for
   about two seconds. Clean digital square wave. See G7 for what follows; B1 is unblocked.
4. ⚠️ **The bench unit is a poor model for the reboot fault.** The battery on VSYS absorbs
   the supply dips production is exposed to, so it is immune to that hypothesis outright. On
   RUN it is **less exposed, not immune**: both boards carry a soldered button, so both have
   a RUN net; production's simply fans out further, to a second button on wires through a
   connector. A clean bench run therefore says little about the reboots, and G6 cannot be
   validated there — the capacitor can be fitted, but there is no fault to suppress. Reboot
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
7. 🔬 **RUN pin pickup — elimination test running on production.** The external reset cable
   is disconnected on the production unit, firmware unchanged, so the board carries exactly
   one changed variable. Record the disconnect date and the prior reboot rate — "none since"
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
   so reboot times are already recorded even before C1 lands — enough to check the
   correlation against a written log of switching events rather than against memory.

   **Characterise the load.** Inductive and electronically-ballasted loads (compressors,
   pumps, LED and fluorescent drivers) produce far worse switching transients than a
   resistive lamp. If the trigger is an LED fixture, driver inrush is a likelier mechanism
   than the switch contact.

   **The connector observation is a red herring. Set it aside.**

   Mating *and* un-mating the reset connector both reset the board, most of the time,
   without the button being pressed. Two hypotheses were built on this and both failed:

   - *Poor noise immunity on a high-impedance RUN node* — withdrawn. Symmetric behaviour in
     both directions of travel is not what charge injection looks like.
   - *Marginal contact, disturbed by vibration* — ruled out by direct test. Knocking the
     wall, the casing, the carrier board and the connector itself, and moving the seated
     connector side to side, produced **no** reboot. The contact is sound.

   What remains is unremarkable: while pins are making or breaking, RUN is briefly shorted or
   left floating, and the board resets. That requires the connector to be *in motion*, which
   never happens in service. The observation therefore says nothing about the in-service
   reboots in either direction, and no further hypotheses should be built on it.

   **Guessing is exhausted; measure instead.** C4 answers the question directly:
   `chip=POR/BOD` means the supply dropped, `chip=RUN` means the RUN line was pulled low.
   One labelled reboot settles what three rounds of hypothesis have not.

   Remaining candidates: **supply sag** (leading by elimination, and still untested — the
   production USB adapter has never been swapped) and **EM pickup on RUN** (possible, no
   evidence either way).

   The running disconnect test loses most of its value now that marginal contact is ruled
   out, but it costs only the reset button, so it can continue until C4 is deployed.

   **Plan: keep the cable disconnected for now, then reconnect it when C4 reaches
   production.** Reset-cause capture is purely observational — it labels reboots, it cannot
   cause or prevent them — so it does not confound anything. But it changes which experiment
   is worth running: direct measurement beats elimination. One labelled reboot reading
   `chip=RUN` or `chip=POR/BOD` settles the question, where weeks of silence against no
   baseline does not. With no positive control, silence is precisely what would otherwise
   need interpreting.

   C4 waits on bench verification rather than on this experiment, because it arrives
   alongside A3/A5/B3/I2, which *do* change behaviour.
8. ✅ **Test target chat type — resolved.** Production is a **group**, and the bench target
   will be a group too. Groups deliver `message` while channels deliver `channel_post`, and
   A1 has not landed — the current parser handles only `channel_post`. A test channel would
   have passed while production quietly failed on `/log`.

   Consequence: **`/log` is expected to be broken on both units until A1 lands.** Every
   update in a group currently raises `KeyError`, which the main loop's catch-all turns into
   a spurious `wlan.disconnect()` and a "back online" message. Do not read that as a new
   fault.
9. **USB adapter quality on production.** A one-minute swap for a known-good supply is the
   cheapest test of the brownout hypothesis and needs no hardware revision. **Raised in
   priority** by the failed reproduction attempt: a cheap adapter sagging under a mains
   transient fits the symptom as well as RUN pickup does, and remains untested.

   The PSU powers **only** the Pico. Two consequences. There is no second device on the
   supply to act as a witness, so a whole-supply dip cannot be confirmed by observing
   something else fail. And the largest load that adapter ever sees is the Pico's own WiFi
   TX bursts (250–300 mA peaks) — a marginal adapter can dip on those alone. A mains sag
   coinciding with a TX burst is a compound trigger that would be rare, unreproducible on
   demand, and still correlated with switching.

10. 🔬 **Common-mode coupling across the optocoupler — hypothesis, speculative.**
    The doorbell is a **Ritto TwinBus**, roughly 24 V, fed from a PSU elsewhere in the
    building — a different circuit from both the Pico and the switched lights. An earlier
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
    an explicit warning that devices with strong magnetic fields — contactors, transformers —
    must not be installed near the power supply or auxiliary units, because induced voltage
    spikes cause malfunctions. Ritto is documenting susceptibility to precisely the kind of
    transient under discussion here.

    **Installation rule worth checking on site.** The handbook requires mains and TwinBus
    wiring to be routed separately to satisfy VDE 0800: 10 cm apart, or with a divider where
    they share a conduit. Older buildings frequently do not respect this. If the separation
    is absent anywhere along the run, the coupling path becomes materially more plausible.

    **Prior art warning.** `tuxuser/ritto_doorbell`, an ESP8266 integration with the same
    TwinBus system, is archived with a note that it never worked reliably. The kind of
    unreliability is unstated, so this is suggestive rather than diagnostic — but it is a
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