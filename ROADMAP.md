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

### A3 — HTTP status checking — **P0**
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

### A5 — Guaranteed response cleanup — **P0**
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

### B1 — IRQ-driven doorbell input — **P0**
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

### B2 — Watchdog — **P0**
`machine.WDT`, fed from the main loop. Timeout must accommodate **both** a slow TLS
handshake (~8 s) **and** a worst-case flash sector erase. Budget generously and *measure*
rather than guess.

A wedged cyw43 stack currently requires someone to physically power-cycle the unit.

### B3 — Fully guarded boot path — **P0**
Configure the GPIO and IRQ **before** touching the network, and wrap the entire startup in
exception handling.

Today `connect_wifi()` runs at module scope outside the `try`, and sends `startupText`
immediately after association — precisely when DNS is least likely to be ready. If that
first send throws, the script dies with a traceback and `doorBellInput` is **never
created**. With no watchdog, the device is dead until someone power-cycles it.

Also: remove the message-sending side effect from inside `connect_wifi()`. Connection and
notification are separate concerns.

### B4 — Bounded WiFi reconnection with backoff — **P1**
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

### C3 — Heartbeat — **P0**
Periodic "alive" ping carrying uptime, free memory, RSSI, alert count and flash write count.
Optionally a `/status` command for on-demand health.

This is the single change that converts silent failure into visible failure. Rank it
alongside B1 in value.

Keep the payload schema **extensible** so battery voltage can be added later without a
breaking change.

---

## D. Security

### D1 — Stop leaking the bot token — **P0, trivial**
Remove `print(url)` from `read_message`, or redact the token.

It currently prints `https://api.telegram.org/bot<TOKEN>/getUpdates?...` on **every poll**.
Anyone pasting Thonny output into a GitHub issue or forum thread hands over full control of
their bot.

### D2 — Secrets file hygiene — **P1**
Ship `secrets.example.py`, gitignore `secrets.py`, remove the tracked file from the docs
flow. Add a startup check that refuses to run on unreplaced placeholders with a clear error.

The README currently instructs users to edit a **tracked** file containing their WiFi
password and bot token.

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

### G3 — Input signal conditioning — **P2**
Document and handle the AC-bell case, where a single PC817 produces a pulse train at mains
frequency rather than a clean level.

Interacts directly with B1's debounce parameters. Decide: solve in software (pulse-train
coalescing) or recommend an RC stretcher on the opto output.

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

> **Verify before relying on it:** the bootrom and SDK reboot path use some of the upper
> registers. Confine to scratch 0–3 and confirm against the specific MicroPython build.

**The one rule that matters: never write flash on a timer.** Everything else follows.

### I2 — Atomic write helper — **P1**
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

### I4 — Filesystem verification — **P3**
Confirm whether the build uses littlefs2 or FAT, and record it in the troubleshooting
section. It changes the endurance envelope by an order of magnitude.

---

## Sequencing

### Phase 1 — Stop the bleeding
`D1` → `A5` → `A3` → `I2` → `B3` → `B1` → `B2` → `B6` → `C3`

Small, surgical, no restructuring. After this the device stops losing presses and stops
failing silently. **The bulk of the early effort belongs here.**

`I2` lands ahead of `A4` because the migration fix needs somewhere safe to write, and the
atomic-write helper is roughly fifteen lines. Establish `I1`'s tier discipline at the same
time — before anyone adds a second `open(..., 'w')` somewhere convenient and quietly starts
writing on a timer.

### Phase 2 — Correctness
`A1` · `A2` · `A4` · `A6` · `A7` · `B4` · `B5` · `C1` · `D2` · `D3` · `E1` · `I1` · `H1`

The device now behaves correctly across all chat types.

### Phase 3 — Restructure
`F1` → `E2` → `F2` → `F3`

**Tests before the refactor, not after.** F1 is what makes E2 safe.

### Phase 4 — Polish and roadmap
`B7` · `B8` · `C2` · `G1` · `G3` · `I3` · `E3` · `E5` · `A8` · `D4` · `F4` · `G4` · `G5` ·
`H2` · `H3`

Then, on future hardware: `E4` · `G2`.

### Dependency edges worth respecting

```
A3 ──▶ A4 ──▶ (needs I2)
A3 ──▶ A6
I2 ──▶ A4, B6-snapshot, I3
B1 ──▶ E5
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
- **BotFather privacy mode** state (D3) — determines the current crash rate in group mode.