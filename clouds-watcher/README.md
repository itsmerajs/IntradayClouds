# Clouds Watcher — entry detection + warm chains (SHADOW)

Built Nov 5 2026 on Raj's approval ("Build the watcher + warm chains").
**Shadow only.** Nothing here sends, wakes, books, or posts. No cron body,
hook, sheet, or live file is touched. The live Entries/Book jobs run
exactly as before; this package only *observes faster* and logs the proof.

## Why

The Entries cron takes ~3 min from a 5-min bar's close to a posted entry:
dispatch lag, then an AI run that re-reads its body, fetches bars, makes
two live Nasdaq quote pulls, and writes the sheet. The watcher moves the
*detection* step into a zero-token Python process that reacts the moment a
bar completes, so a woken worker can start at the quote step.

Measured on the first live day (Nov 5): the frozen scanner subprocess takes
**~8.4 s**; a `--check` pass launched 21 s after a bar completed had
detection finished **29 s after bar close**. In loop form (scan fires ~3 s
after the bar completes) detection lands ≈ bar close + ~12 s.

## Files

| File | What |
|---|---|
| `watcher.py` | Detection passes: `--shadow`, `--check`, `--loop`, `--self-test` |
| `chains.py` | Warm Nasdaq chain cache: `--refresh-once`, `--lookup`, `--self-test` |
| `chains-cache.json` | Per-ticker trimmed chains (front 2 expiries, strikes ±7% of spot) with `fetched_at` |
| `watcher-state.json` | `{date, last_scanned_bar, fired: [ticker\|dir\|level\|bar_time]}` |
| `shadow-log.jsonl` | Append-only evidence log: `signal` / `signal_filtered` / `exit_print` / `pass` lines with second-precision `detected_at` |
| `sandbox/` | Fake HOME for the scanner subprocess (see below) |

## How detection works

1. **Gate**: Mon–Fri 09:25–16:10 ET only; outside, a silent no-op.
2. **Bar math**: the newest completed 5-min bar is `floor5(now) − 5 min`
   (clamped to the 15:55 last bar after the close). A bar is scanned
   exactly once — `watcher-state.json` tracks the last scanned bar.
3. **Sandboxed scanner**: the frozen scanner is *stateful* — running it
   directly would consume the very transitions it reports, starving the
   live Entries cron, and all its paths derive from `expanduser("~")`.
   So each pass copies the live scanner state + daily cache + frozen
   universe into `sandbox/` and runs the scanner with `HOME=sandbox`.
   The live state file is only ever **read** (verified byte-identical
   after the first live passes). Bonus dedupe: once the live Entries run
   consumes a transition, a re-seeded sandbox no longer re-detects it.
4. **Quality pre-filter** (mirrors the lane rule from the alert fields):
   direction agrees with `states.dtrend`, OR dtrend flat AND `states.h1`
   agrees; `states.h1` never against. FIRST/PULLBACK/CONFIRMED kept;
   EXIT prints are logged only (the Book owns exits).
5. **Dedupe**: fired-key set `ticker|dir|level|bar_time` — a signal is
   new exactly once, across both `--shadow` and `--check`.

## Warm chains

`chains.py` pulls the same public Nasdaq chain API `option_quote.py`
uses (stocks, etf fallback), trims to the front two expiries and strikes
within ±7% of spot, and caches bid/ask/last per side. Per-ticker failures
keep the last good entry (staleness stays visible via `fetched_at`); a
Nasdaq 429 stops the pass.

**Measured caveat**: one sequential refresh of all 21 universe names took
**~85 s**, so the earliest-refreshed tickers are ~85 s old when a pass
ends — right at the proposed 90 s freshness bar. Before live wiring, pick
one: (a) tiered refresh — always-on 7 every cycle, the rest rotating; or
(b) a small thread pool (4 workers ≈ ~25 s/pass). The live second pull
stays authoritative either way.

## Proposed live wiring (PROPOSAL — not wired)

- A hook polls `watcher.py --check` every ~30 s during market hours
  (hook script under `~/hooks/scripts/`, per the fleet hook protocol).
  A poll is ~0.2 s when no new bar has completed, ~8.5 s when one has.
  On `{"wake": true, "signals": [...]}` it wakes the Entries worker with
  the payload: ticker, dir, level, price, bar_time, states.
  (Alternative: run `watcher.py --loop` as the supervised process and let
  it emit the same payload — same detection, no poll granularity loss.)
- The woken Entries run changes ONLY in where it starts:
  1. **Skip the scanner run** — consume the watcher's payload instead.
  2. All entry gates run unchanged (quality re-check, premium check,
     liquidity, cooldown, blackout, direction cap, early-confirmation).
  3. **Quote pull #1 = `chains-cache.json`** for the candidate contract
     if that ticker's entry is **< 90 s old** (`chains.py --lookup`);
     otherwise pull #1 is live as today.
  4. **Quote pull #2 = live Nasdaq**, as today. Verified-twice and the
     ≤10% spread cap are unchanged; the cache can never book a number on
     its own — it only removes one network round-trip.
  5. Booking writes (jsonl + sheet row) and the relay proceed exactly as
     the current Entries body prescribes.
- **Cutover evidence**: run `--loop` in shadow for a full session first.
  Compare `shadow-log.jsonl` `detected_at` per signal key against the
  Entries cron's actual booking/relay times for the same signals. Only
  wire the hook if detection beats the cron on every shared signal with
  zero misses and zero extras.

## Self-tests

```
~/workspace/venv-swing/bin/python watcher.py --self-test   # 20 checks
~/workspace/venv-swing/bin/python chains.py  --self-test   # 5 checks
```
Quality accept/reject (10), dedupe fires-once (3), bar-completion
exactly-once (7); chain trim/front-expiry/lookup (5). All passing Nov 5.
