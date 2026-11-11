#!/usr/bin/env python3
"""watcher.py — Raj Intraday Clouds ENTRY watcher (SHADOW MODE).

Raj approved Nov 5 2026: "Build the watcher + warm chains." Goal: cut the
fired->posted time for Clouds entries. Today the Entries cron takes ~3 min
from a 5-min bar's close to a posted entry (dispatch lag + in-run bar fetch
+ two live quote pulls). This watcher detects a new quality signal within
seconds of the bar completing; a future hook (NOT wired — see README.md)
wakes the AI worker on the WAKE payload so it starts at the quote step.

SHADOW MODE — hard scope:
- Writes ONLY inside clouds-watcher/ (state, shadow log, chains cache,
  sandbox). It never sends, wakes, books, or posts anything.
- The live Entries/Book jobs, positions.jsonl, the Positions sheet, every
  cron body, and the frozen universe file are untouched (universe is read).

WHY A SANDBOX: the frozen scanner (bin/raj-intraday-clouds.py, chmod 444)
is STATEFUL — each run consumes the cloud-state transitions it reports
(its state file + audit log live in the desk hidden_files) and its paths
derive from os.path.expanduser("~"). Running it directly from here would
eat signals before the live Entries cron sees them. So each pass:
  1. copies the live scanner state + daily cache + frozen universe into
     sandbox/ (a fake HOME inside clouds-watcher/),
  2. runs the scanner as a subprocess with HOME=sandbox,
  3. parses its stdout JSON, discards the sandbox state afterwards
     (the next pass re-seeds from the then-current live state).
The live lane's state is never written by this watcher, and once the live
Entries run has consumed a transition itself, a re-seeded sandbox no longer
re-detects it — dedupe by construction, plus the fired-key set below.

Modes:
    watcher.py --shadow     one pass: chains refresh + sandboxed scan;
                            new quality signals appended to shadow-log.jsonl
    watcher.py --check      one read-only pass (no state/log writes);
                            prints {"wake": true, "signals": [...]} when a
                            new quality signal is detected — the future
                            hook's poll mode. Chains are NOT refreshed here
                            (hook polls stay fast; the loop/service owns
                            the ~60 s chains cadence).
    watcher.py --loop       shadow passes on the 5-min bar cadence +
                            chains refresh ~every 60 s, market hours only.
                            Still shadow: detections are logged, never sent.
    watcher.py --self-test  synthetic unit checks (no network, no scanner)

Runs under ~/workspace/venv-swing/bin/python (the scanner needs pandas).
Stdlib only for the watcher itself.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chains as chains_mod  # noqa: E402  (same dir)

ET = ZoneInfo("America/New_York")
HOME = Path.home()
DESK = HOME / "workspace/goals/morning-trading-desk/hidden_files"
SCANNER = DESK / "bin" / "raj-intraday-clouds.py"
VENV_PY = HOME / "workspace/venv-swing/bin/python"
UNIVERSE_FILE = DESK / "raj-intraday-clouds-universe.json"
LIVE_STATE = DESK / "raj-intraday-clouds-state.json"
LIVE_DAILY_CACHE = DESK / "raj-intraday-clouds-daily-cache.json"

WATCH_DIR = Path(__file__).resolve().parent
STATE_FILE = WATCH_DIR / "watcher-state.json"
SHADOW_LOG = WATCH_DIR / "shadow-log.jsonl"
LOCK_FILE = WATCH_DIR / "pass.lock"
SANDBOX = WATCH_DIR / "sandbox"
SANDBOX_BASE = SANDBOX / "workspace/goals/morning-trading-desk/hidden_files"

GATE_START = (9, 25)    # lane market-hours gate: Mon–Fri 09:25–16:10 ET
GATE_END = (16, 10)
SESSION_FIRST_BAR = (9, 30)   # first 5-min bar of the session starts 09:30
SESSION_LAST_BAR = (15, 55)   # last one starts 15:55 (completes 16:00)
SCANNER_TIMEOUT_S = 240
ENTRY_LEVELS = ("FIRST", "PULLBACK", "CONFIRMED")
BAR_FMT = "%Y-%m-%d %H:%M ET"


# ---------------------------------------------------------------- gate / bars (pure)
def in_gate(now):
    return now.weekday() < 5 and GATE_START <= (now.hour, now.minute) <= GATE_END


def last_completed_bar_start(now):
    """Start time of the newest COMPLETED 5-min session bar at `now` (ET),
    or None when no session bar has completed yet today.

    A bar stamped 11:45 covers 11:45–11:50 and completes at 11:50, so the
    newest completed bar starts at floor5(now) - 5 min.
    """
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    floored = midnight + timedelta(minutes=(now.hour * 60 + now.minute) // 5 * 5)
    start = floored - timedelta(minutes=5)
    first = now.replace(hour=SESSION_FIRST_BAR[0], minute=SESSION_FIRST_BAR[1],
                        second=0, microsecond=0)
    last = now.replace(hour=SESSION_LAST_BAR[0], minute=SESSION_LAST_BAR[1],
                       second=0, microsecond=0)
    if start < first:
        return None
    if start > last:
        # Past the close: the newest completed bar stays the 15:55 bar
        # (completed 16:00) — the 16:05/16:10 gate-tail passes still owe
        # it exactly one scan.
        return last if now >= last + timedelta(minutes=5) else None
    return start


def bar_str(dt):
    return dt.strftime(BAR_FMT)


# ---------------------------------------------------------------- quality (pure)
def quality_pass(alert):
    """The lane's QUALITY FILTER, mirrored from the alert's own fields:
    direction agrees with states.dtrend, OR dtrend is flat AND states.h1
    agrees; states.h1 is NEVER against. LONG pairs with 'bull', SHORT
    with 'bear'."""
    d = alert.get("dir")
    st = alert.get("states") or {}
    want = {"LONG": "bull", "SHORT": "bear"}.get(d)
    if want is None:
        return False
    if st.get("h1") == ("bear" if want == "bull" else "bull"):
        return False  # 1H against — never quality
    if st.get("dtrend") == want:
        return True
    return st.get("dtrend") == "flat" and st.get("h1") == want


def signal_key(alert):
    return (f"{alert.get('ticker')}|{alert.get('dir')}|{alert.get('level')}"
            f"|{alert.get('time')}")


def partition_new(alerts, fired_keys):
    """Split scanner alerts into (new, keys_to_add). Pure — unit-tested.
    Every printed alert (entry levels + EXIT) is keyed so a re-scan of the
    same bar never re-reports it."""
    new, keys = [], set()
    for a in alerts:
        k = signal_key(a)
        if k in fired_keys or k in keys:
            continue
        keys.add(k)
        new.append(a)
    return new, keys


# ---------------------------------------------------------------- state io
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"date": None, "last_scanned_bar": None, "fired": []}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1) + "\n")
    tmp.replace(STATE_FILE)


def append_shadow_log(rec):
    with SHADOW_LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------- scanner (sandboxed)
def seed_sandbox():
    """Copy the live scanner's state + daily cache + the frozen universe
    into the sandbox HOME. READ-ONLY on the live files (the live scanner
    writes its state via tmp+replace, so a copy is always consistent)."""
    SANDBOX_BASE.mkdir(parents=True, exist_ok=True)
    seeded = []
    for src in (UNIVERSE_FILE, LIVE_STATE, LIVE_DAILY_CACHE):
        if src.exists():
            shutil.copyfile(src, SANDBOX_BASE / src.name)
            seeded.append(src.name)
    return seeded


def run_scanner():
    """Invoke the frozen scanner once, HOME redirected to the sandbox.
    Returns (payload_dict_or_None, elapsed_s, error_str_or_None)."""
    seed_sandbox()
    env = dict(os.environ)
    env["HOME"] = str(SANDBOX)
    t0 = time.time()
    try:
        r = subprocess.run([str(VENV_PY), str(SCANNER)],
                           capture_output=True, text=True,
                           timeout=SCANNER_TIMEOUT_S, env=env, cwd=str(WATCH_DIR))
    except subprocess.TimeoutExpired:
        return None, time.time() - t0, f"scanner timeout >{SCANNER_TIMEOUT_S}s"
    elapsed = time.time() - t0
    payload = None
    for line in reversed((r.stdout or "").strip().splitlines()):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict) and "alerts" in obj:
            payload = obj
            break
    if payload is None:
        tail = (r.stderr or "").strip()[-400:]
        return None, elapsed, f"no JSON payload on scanner stdout; stderr tail: {tail}"
    return payload, elapsed, None


# ---------------------------------------------------------------- one pass
def pass_once(mode, refresh_chains=True):
    """mode: 'shadow' (writes state + shadow log) or 'check' (read-only,
    prints the WAKE payload). Returns the report dict (also printed)."""
    t_start = time.time()
    now = datetime.now(ET)
    report = {"mode": mode, "pass_started_at": now.strftime("%Y-%m-%d %H:%M:%S ET"),
              "status": None}

    if not in_gate(now):
        report["status"] = "outside_gate"
        print(json.dumps(report, indent=1))
        return report

    # overlap guard (shadow only — check mode never writes)
    if mode == "shadow":
        try:
            if LOCK_FILE.exists() and time.time() - float(LOCK_FILE.read_text()) < 300:
                report["status"] = "locked_other_pass_running"
                print(json.dumps(report, indent=1))
                return report
            LOCK_FILE.write_text(str(time.time()))
        except Exception:
            pass

    try:
        latest = last_completed_bar_start(now)
        if latest is None:
            report["status"] = "no_completed_bar_yet"
            print(json.dumps(report, indent=1))
            return report
        report["bar"] = bar_str(latest)

        state = load_state()
        if state.get("date") != now.strftime("%Y-%m-%d"):
            state = {"date": now.strftime("%Y-%m-%d"),
                     "last_scanned_bar": None, "fired": []}
        if state.get("last_scanned_bar") == bar_str(latest):
            report["status"] = "no_new_bar"
            report["elapsed_s"] = round(time.time() - t_start, 2)
            print(json.dumps(report, indent=1))
            return report

        chains_s = None
        if refresh_chains and mode == "shadow":
            c0 = time.time()
            try:
                report["chains"] = chains_mod.refresh_once()
            except Exception as e:
                report["chains"] = {"error": f"{type(e).__name__}: {e}"}
            chains_s = round(time.time() - c0, 2)
            report["chains_s"] = chains_s

        payload, scanner_s, err = run_scanner()
        report["scanner_s"] = round(scanner_s, 2)
        if payload is None:
            # do NOT advance last_scanned_bar — the next pass retries this bar
            report["status"] = "scanner_error"
            report["error"] = err
            report["elapsed_s"] = round(time.time() - t_start, 2)
            print(json.dumps(report, indent=1))
            return report

        alerts = payload.get("alerts") or []
        fired = set(state.get("fired") or [])
        new_alerts, new_keys = partition_new(alerts, fired)
        detected_at = datetime.now(ET)
        quality, filtered, exits = [], [], []
        for a in new_alerts:
            rec = {"ticker": a.get("ticker"), "dir": a.get("dir"),
                   "level": a.get("level"), "price": a.get("price"),
                   "bar_time": a.get("time"), "states": a.get("states")}
            if a.get("level") == "EXIT":
                exits.append(rec)          # log only — the Book owns exits
            elif a.get("level") in ENTRY_LEVELS and quality_pass(a):
                quality.append(rec)
            elif a.get("level") in ENTRY_LEVELS:
                filtered.append(rec)

        report.update({"status": "ok", "alerts_total": len(alerts),
                       "new_quality": quality, "new_filtered": filtered,
                       "new_exits": exits,
                       "detected_at": detected_at.strftime("%Y-%m-%d %H:%M:%S ET"),
                       "elapsed_s": round(time.time() - t_start, 2)})

        if mode == "shadow":
            for rec in quality:
                append_shadow_log({"type": "signal",
                                   "detected_at": report["detected_at"],
                                   "bar_time": rec["bar_time"],
                                   "ticker": rec["ticker"], "dir": rec["dir"],
                                   "level": rec["level"], "price": rec["price"],
                                   "states": rec["states"],
                                   "scanner_s": report["scanner_s"]})
            for rec in filtered:
                append_shadow_log({"type": "signal_filtered",
                                   "detected_at": report["detected_at"],
                                   "bar_time": rec["bar_time"],
                                   "ticker": rec["ticker"], "dir": rec["dir"],
                                   "level": rec["level"], "price": rec["price"],
                                   "states": rec["states"]})
            for rec in exits:
                append_shadow_log({"type": "exit_print",
                                   "detected_at": report["detected_at"],
                                   "bar_time": rec["bar_time"],
                                   "ticker": rec["ticker"], "dir": rec["dir"],
                                   "price": rec["price"]})
            append_shadow_log({"type": "pass",
                               "detected_at": report["detected_at"],
                               "bar": report["bar"], "alerts_total": len(alerts),
                               "new_quality": len(quality),
                               "new_filtered": len(filtered),
                               "new_exits": len(exits),
                               "scanner_s": report["scanner_s"],
                               "chains_s": chains_s,
                               "elapsed_s": report["elapsed_s"]})
            state["fired"] = sorted(fired | new_keys)
            state["last_scanned_bar"] = bar_str(latest)
            state["updated_at"] = report["detected_at"]
            save_state(state)
        else:  # check — the future hook's WAKE payload
            report["wake"] = bool(quality)
            if quality:
                report["signals"] = quality
        print(json.dumps(report, indent=1))
        return report
    finally:
        if mode == "shadow":
            LOCK_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------- loop
def loop():
    """Shadow loop: chains ~every 60 s; one scan pass per new 5-min bar,
    fired ~3 s after the bar completes. Detections are logged only."""
    last_chain = 0.0
    while True:
        now = datetime.now(ET)
        if in_gate(now):
            if time.time() - last_chain >= 60:
                try:
                    chains_mod.refresh_once()
                except Exception:
                    pass
                last_chain = time.time()
            pass_once("shadow", refresh_chains=False)
            secs_into = (now.minute % 5) * 60 + now.second
            wait = 300 - secs_into + 3
            if wait <= 0:
                wait += 300
            time.sleep(max(5, wait))
        else:
            time.sleep(60)


# ---------------------------------------------------------------- self-test
def self_test():
    fails = 0

    def check(name, good, got):
        nonlocal fails
        print(f"{'PASS' if good else 'FAIL'}  {name}: {got}")
        fails += 0 if good else 1

    def A(d, dtrend, h1):
        return {"ticker": "TST", "dir": d, "level": "FIRST", "price": 100.0,
                "time": "2026-11-05 10:00 ET",
                "states": {"dtrend": dtrend, "h1": h1,
                           "m5_12": "flat", "m34_50": "flat"}}

    q_cases = [
        ("LONG daily bull + 1H bull", A("LONG", "bull", "bull"), True),
        ("LONG daily bull + 1H flat", A("LONG", "bull", "flat"), True),
        ("LONG daily flat + 1H bull", A("LONG", "flat", "bull"), True),
        ("LONG daily bear (against)", A("LONG", "bear", "bull"), False),
        ("LONG 1H bear (against)", A("LONG", "bull", "bear"), False),
        ("LONG all flat", A("LONG", "flat", "flat"), False),
        ("SHORT daily bear + 1H bear", A("SHORT", "bear", "bear"), True),
        ("SHORT daily flat + 1H bear", A("SHORT", "flat", "bear"), True),
        ("SHORT daily bull (against)", A("SHORT", "bull", "bear"), False),
        ("SHORT 1H bull (against)", A("SHORT", "bear", "bull"), False),
    ]
    for name, alert, want in q_cases:
        got = quality_pass(alert)
        check(f"quality {name}", got == want, got)

    a1 = {"ticker": "AAA", "dir": "LONG", "level": "FIRST",
          "time": "2026-11-05 10:00 ET", "price": 10.0, "states": {}}
    a2 = {"ticker": "BBB", "dir": "SHORT", "level": "PULLBACK",
          "time": "2026-11-05 10:00 ET", "price": 20.0, "states": {}}
    a3 = {"ticker": "AAA", "dir": "LONG", "level": "EXIT",
          "time": "2026-11-05 10:05 ET", "price": 10.5, "states": {}}
    new1, keys1 = partition_new([a1, a2, a3], set())
    new2, keys2 = partition_new([a1, a2, a3], keys1)
    check("dedupe: first pass all new", len(new1) == 3, len(new1))
    check("dedupe: second pass none new (fires once)",
          len(new2) == 0 and keys2 == set(), (len(new2), keys2))
    check("dedupe: key format ticker|dir|level|bar_time",
          signal_key(a1) == "AAA|LONG|FIRST|2026-11-05 10:00 ET", signal_key(a1))

    def at(h, m, s=0):
        return datetime(2026, 11, 5, h, m, s, tzinfo=ET)

    bar_cases = [
        ("09:34 -> none (no completed session bar)", at(9, 34), None),
        ("09:35 -> 09:30 bar", at(9, 35), "2026-11-05 09:30 ET"),
        ("09:36:30 -> 09:30 bar", at(9, 36, 30), "2026-11-05 09:30 ET"),
        ("11:59:59 -> 11:50 bar", at(11, 59, 59), "2026-11-05 11:50 ET"),
        ("12:03 -> 11:55 bar", at(12, 3), "2026-11-05 11:55 ET"),
        ("16:07 -> 15:55 bar (last)", at(16, 7), "2026-11-05 15:55 ET"),
    ]
    for name, now, want in bar_cases:
        got_dt = last_completed_bar_start(now)
        got = bar_str(got_dt) if got_dt else None
        check(f"bar {name}", got == want, got)
    # triggers exactly once: same `now`, state advanced -> no second scan
    latest = last_completed_bar_start(at(12, 3))
    state = {"last_scanned_bar": bar_str(latest)}
    check("bar: no re-scan once state advanced",
          state["last_scanned_bar"] == bar_str(last_completed_bar_start(at(12, 3))),
          state["last_scanned_bar"])
    total = len(q_cases) + 3 + len(bar_cases) + 1
    print(f"watcher self-test: {total - fails}/{total} passed")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--shadow", action="store_true")
    g.add_argument("--check", action="store_true")
    g.add_argument("--loop", action="store_true")
    g.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.loop:
        loop()
        return 0
    pass_once("shadow" if a.shadow else "check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
