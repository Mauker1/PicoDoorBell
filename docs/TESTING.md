# Bench testing

Manual checks to run on the prototype before promoting a build to production.
Host-side tests cover logic; these cover the things only hardware can answer.

## Before the bench: run the gate

```
python3 tools/check.py
```

Trailing newlines, byte-compilation, roadmap structure, and every suite in `tests/`. Exits
non-zero on any failure, so it works as a pre-commit hook.

A syntax error caught here is a minute; the same error on a board is a device that will not
start, found by walking to the entryway.

---

**Setup:** bench unit on USB power (not battery — it masks supply behaviour),
`board.py` copied from `boards/board_prototype.py` (GP18), bench `secrets.py`,
Thonny attached.

---

## Before you start: the watchdog changes the bench workflow

Once B2 is running, **Ctrl-C resets the board a few seconds later**. Leaving the
main loop stops the feeding, and an RP2040 watchdog cannot be disarmed. The
firmware prints a warning on the way out.

That is correct in service — an exited loop is a dead doorbell — but it means you
can no longer stop the script and stay at the REPL. To get a REPL, stop the
script and reconnect after the reset, or hold the board in the bootloader.

Every Ctrl-C also increments the boot counter and reads `verdict=watchdog` on the next
boot -- it is a real timeout, since nothing is feeding once the loop exits.
Expected, not a fault.

---

## B1 — IRQ-driven input

Simulate a ring by applying 5 V to the input, as with the bench supply.

### 1. Boot

```
Doorbell input ready on GP18
Watchdog armed at 8000ms
```

Both lines must appear. No pin message means `board.py` is missing or wrong.

### 2. A normal ring

Apply 5 V for about two seconds.

- Console: `Doorbell ring, ~2000ms` — the measured width, which should match the
  ~2 s from the G7 measurement
- Telegram: `Doorbell activated!` within a couple of seconds

**The width in that log line is the point.** It confirms both edges were captured
in hardware rather than inferred.

### 3. A transient is rejected

Tap 5 V on and off as briefly as you can, well under 150 ms.

- Console: `Doorbell transient ignored, NNms`
- Telegram: **nothing**

If a short tap produces an alert, `MIN_PULSE_MS` is not doing its job — which
also means EMI on the line could produce phantom notifications.

If your tap is too slow to land under 150 ms, temporarily lower `MIN_PULSE_MS` to
something like 400 and retry, then put it back.

### 4. One ring, one alert

Ring, wait two seconds, ring again — both within the 5 s lockout.

- Telegram: **one** message, not two

Then ring twice more with at least 8 s between them: **two** messages.

### 5. A ring during a blocking call

The loop prints `Checking for new messages...` every 60 seconds and blocks for a
second or two. Ring in that window.

- Telegram: the alert still arrives

This is the case the old polling loop could lose. Getting the timing right by hand
is fiddly; several attempts across a few minutes is enough to be reassuring.

### 6. Stuck input

Hold 5 V on for more than 15 seconds.

- Console: `Doorbell input stuck high`
- Telegram: **nothing**

Then release and ring normally. It should behave as usual — a stuck input must not
wedge the input permanently.

### 7. Quiet line

Leave it alone for ten minutes with nothing connected to the input.

- No ring messages at all

Any unprompted alert means noise is getting in, which would be worth knowing
given the open EMI question.

---

## B2 — Watchdog

### 8. It is armed

`Watchdog armed at 8000ms` during boot. If you see `Watchdog unavailable: ...`
instead, the board is running unguarded — deliberate, but worth knowing.

### 9. It actually bites

Press Ctrl-C. The firmware prints its warning; **the board should reset within
about 8 seconds**, and the next boot should report `verdict=watchdog`.

This is the decisive test. A board that does not reset here has an armed watchdog
that is being fed by something it should not be, or was never armed at all.

### 10. It does not bite during normal work

Leave it running **at least 15 minutes**, spanning several `Checking for new
messages...` cycles and a few rings.

- No unexplained resets
- The boot counter does not advance

A reset here means feeding is insufficient somewhere — note what the console
showed immediately before it.

### 11. It does not bite during a network outage

Turn the router off for two or three minutes.

- Console: repeated `WiFi is disconnected. Trying to connect.`
- **No reset**

`connect_wifi()` feeds while retrying, because resetting cannot fix an absent
router. Restore the router; it should reconnect and announce itself.

### 12. It survives the error path

Hardest to force deliberately. If any error does occur, the 10 s grace period runs
in fed slices and must **not** reset the board. Watch for a reset following any
error message.

---

## Currently under test

`ROADMAP.md` marks these 🧪 — flashed to the bench but not yet verified. Each needs
something a short session cannot provide:

| Item | Waiting on |
| --- | --- |
| `A6` | A log long enough to split across messages. Send `/log` only after a busy stretch. |
| `A7` | A backlog to discard. Send a command while the board is rebooting, then look for `Discarded N stale update(s)`. |
| `C3` | Days of heartbeats. One reading is a baseline; a flat trend is the verification. |

## Sign-off before promotion

**Not everything below needs re-running every time.** Most of it was verified on builds
the current tree descends from, with the relevant code untouched since. Re-test what
changed, plus anything still marked 🧪.

### Changed since it was last verified — re-run these

| Check | Expect | Why |
| --- | --- | --- |
| Boot | `WiFi power save off`, connection on attempt 1 or 2 | `B8` is new |
| Console during a quiet minute | **No** `Checking for new messages` | `C3` silenced it |
| `/status` | A full report | New command |
| Ring, drop WiFi, restore | Delivered marked `(delayed Ns)` | The queue gained an immediate snapshot on a degraded network |
| Drop WiFi for a few minutes | `WiFi down (...)` every 10 s, no flood, then recovery | `B4`'s progress states and intervals changed |
| Ctrl-C | Reset within ~8 s; next boot reads **`verdict=watchdog`** | Was `warm-reset` before the TIMER bit was decoded |

### Still under test — see *Currently under test* above

`A6` needs a log past 4096 characters. `A7` needs a backlog to discard. `C3` needs days of
heartbeats.

### Verified and unchanged — no need to repeat

Ring detection and width measurement, transient rejection at 150 ms, one alert per ring
inside the lockout, stuck-input handling and recovery, no phantom rings on a quiet line, the
watchdog arming, the flash write at the 60 s gate, and the queue surviving a reset.

### The one that only time can answer

Free memory, read from `/status` rather than by killing the run. **The baseline for this
build is 159,968 bytes** — not the 178,480 an earlier build reported, which was a smaller
binary. A single reading proves nothing; a flat figure across several heartbeats is the
verification.

---

## Promoting to production

1. Back up the current `main.py` on the device under another name.
2. Copy `boards/board_v1_2.py` to the device as `board.py` (**GP16**).
3. Leave production's `secrets.py` alone.
4. Flash `main.py`.
5. Confirm `Doorbell input ready on GP16` and a startup message in the channel.
6. Ring the real doorbell.
7. **Jab it as briefly as possible** and read the logged width. This is the only
   way to learn the minimum a real ring can produce, and it decides whether
   `MIN_PULSE_MS` can safely be raised above 150. Bench figures cannot answer it:
   they describe a wire being touched by hand, not terminal 04.

**Discard the first boot** from the reboot dataset: with no magic word yet
written, a soft reboot is indistinguishable from a power-on and reads as
`verdict=power`. Only ever affects the first boot after a flash.