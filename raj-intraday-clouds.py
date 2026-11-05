#!/usr/bin/env python3
"""Raj Intraday Clouds — SHADOW alert scanner (standalone, Oct 27 2026).

Raj's explicit build order (chat, Oct 27 2026): a NEW standalone intraday
job, separate from the MTF swing scanner (mtf-swing-scan.py is NOT touched).
Alerts fire from day one but are paper/shadow-tracked and graded; every
card built from this output must be labeled SHADOW.

Cloud logic copied from mtf-swing-scan.py raj_clouds()/_cloud_state():
  state = 'bull' if fast EMA > slow EMA AND fast rising (last vs previous)
          'bear' if fast EMA < slow EMA AND fast falling
          else 'flat'. Completed bars only.
Raj's 4 rules, intraday mapping:
  1. 5/12 curl (on 5-MIN bars)   -> entry / first signal  (FIRST alert)
  2. 34/50 curl (on 5-MIN bars)  -> stronger confirmation (CONFIRMED upgrade)
  3. 1HR MTF (hourly EMA 8/21)   -> hold guidance; flip against = EXIT alert
  4. Daily trend (EMA21>EMA20 AND EMA55>EMA50 -> bull, inverse -> bear)
                                 -> day bias gate for FIRST alerts
  5. PULLBACK (Raj's order, Oct 30 2026): while a leg is open, a 5/12
     reading against/flat arms a pullback; the 5/12 re-curling INTO the
     leg's direction — with the 1HR cloud agreeing and the daily trend
     not opposing — fires a PULLBACK entry alert on the same leg. The
     entry price must also improve on the leg's last entry (staircase):
     a re-curl that does not progress past the last entry is chop.

Universe: SPY, QQQ, IWM, TSLA, NVDA, MSFT, AMZN (always-on, Raj Oct 27 2026)
+ tickers parsed from today's Morning Desk report
(newest ~/workspace/morning-desk/reports/YYYY-MM-DD.md watchlist / top-ideas
sections). Cap 25.

CANDIDATE (Nov 2 2026): speed-only variant of the Oct 27 scanner, built
under Raj's "reduce time not quality" order. Differences vs the original
are confined to (1) a per-trading-day cache of the completed DAILY frame
(the only series whose completed() output is provably frozen intraday —
see _daily_bars_cached), (2) a wider fetch pool (pure scheduling;
pool.map preserves universe order and every fetch is independent), and
(3) LEVELS instrumentation (see _levels_for): one extra premarket fetch
per ticker and 8 extra fields on entry records — no gate, target, or
trigger reads them.
Plus ONE intentional behavior change, Raj's order Nov 2 2026 15:34 ET:
(4) the after-3PM sure-shot gate (see _sure_shot_blocked) — from
15:00 ET, FIRST/PULLBACK entries book only when the daily trend and
the 1H cloud both align with the direction; blocked signals are
logged only. Everything else in this file is behavior-identical.
The 1h/5m series are fetched fresh every run, exactly as the original:
their Yahoo responses include the still-forming bar, so their values can
change at any moment. All logic, gates, state handling and outputs are
the original's, unchanged.

VOLUME CANDIDATE (Nov 4 2026 — Raj approved in chat, "Go as
recommended"): instrumentation ONLY — no gate, target, trigger, stop,
or exit reads any of these values, and no existing field changes
meaning except vol_ratio's measurement basis. Differences vs the live
scanner are confined to _levels_for and its three call sites:
(a) vol_ratio now uses COMPLETED 5m bars only; (b) PULLBACK records
gain pb_vol_ratio / pb_vol_tag (pullback volume dry-up proxy);
(c) all entry records gain day_pace_ratio / day_pace_tag (cumulative
session volume vs the 20-session average pace). NOT swapped into live.

Output: ONE JSON object on stdout:
  {"quiet": bool, "alerts": [...], "grades": [...],
   "universe": [...], "notes": [...]}
All diagnostics go to stderr. Never raises.
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
BASE = os.path.expanduser("~/workspace/goals/morning-trading-desk/hidden_files")
STATE_FILE = os.path.join(BASE, "raj-intraday-clouds-state.json")
AUDIT_FILE = os.path.join(BASE, "raj-intraday-clouds-alerts.jsonl")
REPORTS_DIR = os.path.expanduser("~/workspace/morning-desk/reports")
BASE_TICKERS = ["SPY", "QQQ", "IWM", "TSLA", "NVDA", "MSFT", "AMZN"]
MAX_UNIVERSE = 25
YH_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
DAILY_CACHE_FILE = os.path.join(
    BASE, "raj-intraday-clouds-daily-cache.json")
POOL_WORKERS = 16


def now_et():
    return dt.datetime.now(ET)  # DST-safe (same helper as mtf-swing-scan.py)


def log(msg):
    print(f"[raj-clouds {now_et().strftime('%H:%M:%S')}] {msg}",
          file=sys.stderr, flush=True)


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


# ---------------------------------------------------------------- Yahoo bars
def yahoo_bars(ticker, interval, range_, prepost=False):
    """Fetch OHLCV via Yahoo chart API. Returns DataFrame (UTC index) or None.
    Same approach as mtf-swing-scan.py's yahoo_bars. prepost=True appends
    includePrePost (LEVELS instrumentation fetch only — the default URL is
    byte-identical to the original scanner's)."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?interval={interval}&range={range_}&events=div%2Csplit"
           + ("&includePrePost=true" if prepost else ""))
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=YH_UA)
            raw = urllib.request.urlopen(req, timeout=15).read()
            j = json.loads(raw)
            r = j["chart"]["result"][0]
            ts = r["timestamp"]
            q = r["indicators"]["quote"][0]
            df = pd.DataFrame({
                "Open": q.get("open"), "High": q.get("high"),
                "Low": q.get("low"), "Close": q.get("close"),
                "Volume": q.get("volume"),
            }, index=pd.to_datetime(ts, unit="s", utc=True))
            df = df.dropna(subset=["Close"])
            if len(df) < 5:
                return None
            return df
        except Exception as e:
            if attempt == 2:
                log(f"yahoo_bars {ticker} {interval} failed: {e}")
            time.sleep(1 + attempt * 2)
    return None


def completed(df, bar_len):
    """Convert index to ET and drop a still-forming last bar
    (bar start + bar length > now ET = incomplete)."""
    if df is None or len(df) == 0:
        return df
    d = df.copy()
    d.index = d.index.tz_convert(ET)
    now = now_et()
    if d.index[-1] + bar_len > now:
        d = d.iloc[:-1]
    return d


# ------------------------------------------------------- daily-frame cache
# SPEED (Raj's order, Nov 2 2026 — "reduce time not quality"): the daily
# series is the ONLY one whose completed() output cannot change during a
# trading day. Verified on recorded Yahoo responses (Nov 2 2026): daily
# bars are stamped 09:30 ET and carry NO appended live-quote point, so
# completed() drops today's forming bar on every run (09:30 + 1 day > now
# all day) and the completed frame is exactly the sessions through
# yesterday — immutable history. The 1h and 5m responses DO carry an
# appended zero-volume quote point stamped at fetch time; completed()
# drops that point and KEEPS the still-forming regular bar with live
# values, so the 1h cloud (and everything on 5m) can change at any
# moment — those series are NEVER cached. The cache stores the completed
# daily frame per ticker, keyed by trading day, and is written only when
# the raw response's last bar is today's (proof the dropped bar was
# today's, so the completed frame is the full set of prior sessions);
# any doubt -> no write, and the next run simply fetches again, exactly
# like the original. Cache failures never affect outputs, only speed.
_DAILY_CACHE = {"day": None, "tickers": {}}
_DAILY_PENDING = {}


def _load_daily_cache(today):
    _DAILY_CACHE["day"] = today
    _DAILY_CACHE["tickers"] = {}
    try:
        if os.path.exists(DAILY_CACHE_FILE):
            with open(DAILY_CACHE_FILE) as f:
                data = json.load(f)
            if data.get("day") == today:
                _DAILY_CACHE["tickers"] = data.get("tickers", {})
    except Exception as e:
        log(f"daily cache read failed (ignored): {e}")


def _save_daily_cache():
    if not _DAILY_PENDING:
        return
    try:
        tickers = dict(_DAILY_CACHE.get("tickers") or {})
        tickers.update(_DAILY_PENDING)
        tmp = DAILY_CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"day": _DAILY_CACHE.get("day"),
                       "tickers": tickers}, f)
        os.replace(tmp, DAILY_CACHE_FILE)
        _DAILY_CACHE["tickers"] = tickers
        _DAILY_PENDING.clear()
    except Exception as e:
        log(f"daily cache write failed (ignored): {e}")


def _frame_to_cache(df):
    """Serialize a completed daily frame (ET index) for the cache."""
    cols = {}
    for c in ("Open", "High", "Low", "Close", "Volume"):
        cols[c] = [None if pd.isna(v) else float(v) for v in df[c]]
    return {"ts": [int(x.timestamp()) for x in df.index], "cols": cols}


def _frame_from_cache(ent):
    """Rebuild the completed daily frame stored by _frame_to_cache."""
    idx = pd.to_datetime(ent["ts"], unit="s", utc=True).tz_convert(ET)
    data = {c: [np.nan if v is None else v for v in vals]
            for c, vals in ent["cols"].items()}
    return pd.DataFrame(data, index=idx)


def _daily_bars_cached(t, today):
    """Completed daily bars for t — served from the day cache when that
    provably equals the frame a fresh fetch would produce, else fetched
    (and cached) exactly as the original fetches them."""
    ent = (_DAILY_CACHE.get("tickers") or {}).get(t)
    if ent is not None:
        try:
            df = _frame_from_cache(ent)
            if len(df) and df.index[-1].strftime("%Y-%m-%d") < today:
                return df
        except Exception as e:
            log(f"daily cache entry bad for {t} (refetching): {e}")
    raw = yahoo_bars(t, "1d", "1y")
    if raw is None:
        return None
    raw_last = raw.index[-1].tz_convert(ET).strftime("%Y-%m-%d")
    out = completed(raw, dt.timedelta(days=1))
    if raw_last == today and out is not None and len(out):
        try:
            _DAILY_PENDING[t] = _frame_to_cache(out)
        except Exception as e:
            log(f"daily cache store failed for {t} (ignored): {e}")
    return out


# ------------------------------------------------- levels instrumentation
# LEVELS (Raj's order, Nov 2 2026 — "add all good once", instrumentation
# ONLY: no gates, no targets, no trigger changes; nothing in the signal
# logic reads these values). Objective levels attached to entry records
# (FIRST / PULLBACK / CONFIRMED) so grading can split results by room and
# volume context:
#   pdh/pdl — prior completed daily session's high/low, from the daily
#             frame the scanner already fetches (its last completed bar).
#   pmh/pml — today's premarket (04:00–09:29 ET) high/low, from ONE extra
#             5m fetch per ticker with includePrePost (no existing fetch
#             returns premarket bars — the standard URLs are regular
#             session only). Null when Yahoo returns no premarket bars.
#   room_up_pct / room_down_pct — % distance from the entry price to the
#             nearest level above / below (nearest of the four levels on
#             each side); null when no level exists on that side.
#   vol_ratio — COMPLETED-BAR BASIS (volume candidate, Nov 4 2026):
#             the last COMPLETED 5m bar's volume over the mean volume
#             of the prior 20 completed bars. The m5 frame's last row
#             is the still-forming entry bar (entries fire intrabar by
#             design), whose partial volume biased the old
#             as-of-the-run ratio low, so it is excluded here; null
#             when fewer than 21 completed bars are available or the
#             volume data is incomplete. vol_tag = "spike" when
#             vol_ratio >= 1.5, else "normal"; null when vol_ratio
#             is null.
#   day_pace_ratio / day_pace_tag (volume candidate, Nov 4 2026; all
#             entry records) — cumulative regular-session volume today
#             through the last completed 5m bar, over the mean total
#             daily volume of the last 20 completed daily sessions
#             scaled by the elapsed fraction of the 390-minute session
#             at that bar's close. day_pace_tag = "in-play" when the
#             ratio >= 1.5, else "normal"; both null when the daily
#             frame or today's completed bars are incomplete.
#   pb_vol_ratio / pb_vol_tag (volume candidate, Nov 4 2026; PULLBACK
#             records ONLY) — documented PROXY for pullback volume
#             dry-up: mean volume of the 3 completed bars immediately
#             before the entry bar (late pullback bars) over the mean
#             volume of the 3 completed bars before those (impulse
#             bars). pb_vol_tag = "dry" when ratio < 0.8, "heavy" when
#             >= 1.2, else "normal"; both null when fewer than 6
#             completed bars precede the entry bar or the data is
#             incomplete.
def _levels_for(daily_df, prepost_df, m5_df, entry_price, today,
                level=None):
    out = {"pdh": None, "pdl": None, "pmh": None, "pml": None,
           "room_up_pct": None, "room_down_pct": None,
           "vol_ratio": None, "vol_tag": None,
           "day_pace_ratio": None, "day_pace_tag": None}
    if level == "PULLBACK":
        out["pb_vol_ratio"] = None
        out["pb_vol_tag"] = None
    if daily_df is not None and len(daily_df):
        try:
            hi = float(daily_df["High"].iloc[-1])
            lo = float(daily_df["Low"].iloc[-1])
            if not pd.isna(hi):
                out["pdh"] = round(hi, 2)
            if not pd.isna(lo):
                out["pdl"] = round(lo, 2)
        except Exception:
            pass
    if prepost_df is not None and len(prepost_df):
        try:
            d = prepost_df.copy()
            d.index = d.index.tz_convert(ET)
            mins = d.index.hour * 60 + d.index.minute
            win = d[(d.index.strftime("%Y-%m-%d") == today)
                    & (mins >= 4 * 60) & (mins < 9 * 60 + 30)]
            his = win["High"].dropna()
            los = win["Low"].dropna()
            if len(his):
                out["pmh"] = round(float(his.max()), 2)
            if len(los):
                out["pml"] = round(float(los.min()), 2)
        except Exception:
            pass
    if entry_price:
        lv = [out[k] for k in ("pdh", "pdl", "pmh", "pml")
              if out[k] is not None]
        above = [v for v in lv if v > entry_price]
        below = [v for v in lv if v < entry_price]
        if above:
            out["room_up_pct"] = round(
                (min(above) - entry_price) / entry_price * 100, 2)
        if below:
            out["room_down_pct"] = round(
                (entry_price - max(below)) / entry_price * 100, 2)
    if m5_df is not None and len(m5_df):
        try:
            # Completed 5m bars only (volume candidate, Nov 4 2026):
            # the frame's last row is the still-forming entry bar, so
            # the completed series is every row before it.
            comp = m5_df.iloc[:-1]
            vols = comp["Volume"]
            # (a) vol_ratio on the completed-bar basis: last completed
            # bar's volume over the mean of the prior 20 completed bars.
            if len(comp) >= 21:
                cur = vols.iloc[-1]
                prior = vols.iloc[-21:-1].dropna()
                if not pd.isna(cur) and len(prior) == 20:
                    base = float(prior.mean())
                    if base > 0:
                        r = float(cur) / base
                        out["vol_ratio"] = round(r, 2)
                        out["vol_tag"] = ("spike" if r >= 1.5
                                          else "normal")
            # (b) PULLBACK dry-up proxy (PULLBACK records only): the 3
            # completed bars immediately before the entry bar (late
            # pullback) over the 3 completed bars before those (impulse).
            if level == "PULLBACK" and len(comp) >= 6:
                late = vols.iloc[-3:].dropna()
                impulse = vols.iloc[-6:-3].dropna()
                if len(late) == 3 and len(impulse) == 3:
                    ib = float(impulse.mean())
                    if ib > 0:
                        r = float(late.mean()) / ib
                        out["pb_vol_ratio"] = round(r, 2)
                        out["pb_vol_tag"] = ("dry" if r < 0.8 else
                                             "heavy" if r >= 1.2
                                             else "normal")
            # (c) day-pace (RVOL) tag: today's cumulative regular-
            # session volume through the last completed bar, over the
            # mean total daily volume of the last 20 completed daily
            # sessions scaled by the elapsed fraction of the session.
            if len(comp) and daily_df is not None \
                    and len(daily_df) >= 20:
                last_bar = comp.index[-1]
                if last_bar.strftime("%Y-%m-%d") == today:
                    dvol = daily_df["Volume"].tail(20).dropna()
                    todays = comp[comp.index.strftime("%Y-%m-%d")
                                  == today]
                    mins = todays.index.hour * 60 + todays.index.minute
                    todays = todays[(mins >= 9 * 60 + 30)
                                    & (mins < 16 * 60)]
                    tvol = todays["Volume"]
                    elapsed = ((last_bar.hour * 60 + last_bar.minute
                                + 5) - (9 * 60 + 30))
                    frac = min(max(elapsed, 0), 390) / 390.0
                    if len(dvol) == 20 and len(tvol) \
                            and bool(tvol.notna().all()):
                        avg_day = float(dvol.mean())
                        if avg_day > 0 and frac > 0:
                            r = float(tvol.sum()) / (avg_day * frac)
                            out["day_pace_ratio"] = round(r, 2)
                            out["day_pace_tag"] = ("in-play" if r >= 1.5
                                                   else "normal")
        except Exception:
            pass
    return out


# ------------------------------------------- after-3PM sure-shot gate
# BEHAVIOR CHANGE (Raj's order, Nov 2 2026 15:34 ET — "keep tiny trades
# after 3pm take only if it is sure shot"; exact definition confirmed by
# his option tap 15:35 ET): from 15:00 ET, a new entry (FIRST or
# PULLBACK) books ONLY when the daily trend AND the 1H cloud BOTH align
# with the trade direction — flat on either blocks it. A blocked signal
# is logged only (stderr + a stdout note naming the gate), never emitted
# as an entry record; on a blocked PULLBACK the leg's pb_seen/last_entry
# are left untouched. The gate reads the entry's bar timestamp — the
# same timestamp the scanner assigns the entry record — and the SAME
# states dict the existing quality gates read; no second, divergent
# computation. Exits, CONFIRMED upgrades, the ladder, cooldowns and
# every pre-15:00 path are untouched. (Add-on booking is a caller/worker
# rule; the scanner has no add-on record type, so the no-add-ons half of
# Raj's rule has no scanner-side surface — CONFIRMED is an upgrade of an
# existing entry, not an add-on, and is unchanged.)
def _sure_shot_blocked(states, d, bar_time):
    """Reason string when the after-3PM sure-shot gate blocks an entry,
    else None."""
    if bar_time.hour * 60 + bar_time.minute < 15 * 60:
        return None
    want = "bull" if d == "LONG" else "bear"
    if states["dtrend"] == want and states["h1"] == want:
        return None
    return (f"daily {states['dtrend']}, 1H {states['h1']} — "
            f"not both {want}")


# ---------------------------------------------------------------- cloud logic
# Copied from mtf-swing-scan.py _cloud_state() / raj_clouds() (Oct 19 2026).
def _cloud_state(fast, slow):
    """'bull' if fast>slow and fast rising; 'bear' if fast<slow and falling;
    else 'flat'. fast/slow are EMA series (last value = latest completed)."""
    try:
        f0, f1 = float(fast.iloc[-1]), float(fast.iloc[-2])
        s0 = float(slow.iloc[-1])
        if np.isnan(f0) or np.isnan(f1) or np.isnan(s0):
            return "flat"
        if f0 > s0 and f0 > f1:
            return "bull"
        if f0 < s0 and f0 < f1:
            return "bear"
        return "flat"
    except Exception:
        return "flat"


def daily_trend(daily_closes):
    """Rule 4, copied from raj_clouds(): EMA21>EMA20 AND EMA55>EMA50 -> bull;
    inverse -> bear; else flat. Needs >= 56 daily closes."""
    dc = pd.Series(daily_closes, dtype=float).dropna()
    if len(dc) < 56:
        return "flat"
    e20, e21 = ema(dc, 20), ema(dc, 21)
    e50, e55 = ema(dc, 50), ema(dc, 55)
    bull = (float(e21.iloc[-1]) > float(e20.iloc[-1])
            and float(e55.iloc[-1]) > float(e50.iloc[-1]))
    bear = (float(e21.iloc[-1]) < float(e20.iloc[-1])
            and float(e55.iloc[-1]) < float(e50.iloc[-1]))
    return "bull" if bull else ("bear" if bear else "flat")


def ticker_states(daily_df, hourly_df, m5_df):
    """All four cloud states for one ticker from completed bars."""
    out = {"dtrend": "flat", "m5_12": "flat", "m34_50": "flat", "h1": "flat"}
    if daily_df is not None and len(daily_df) > 0:
        out["dtrend"] = daily_trend(daily_df["Close"])
    if hourly_df is not None:
        hc = hourly_df["Close"].dropna()
        if len(hc) >= 25:  # same warmup as mtf raj_clouds h1
            out["h1"] = _cloud_state(ema(hc, 8), ema(hc, 21))
    if m5_df is not None:
        mc = m5_df["Close"].dropna()
        if len(mc) >= 14:
            out["m5_12"] = _cloud_state(ema(mc, 5), ema(mc, 12))
        if len(mc) >= 52:
            out["m34_50"] = _cloud_state(ema(mc, 34), ema(mc, 50))
    return out


# ---------------------------------------------------------------- universe
_SECTION_KEYS = ("WATCHLIST", "TOP IDEAS", "TOP 3 OPPORTUNITIES", "BATTLE PLAN")
_TICKER_LINE = re.compile(
    r"^\s*(?:\d+\.\s+|-\s+)?([A-Z]{1,5})(?:\s*·|\s+—|\s+–|\s+-\s)")
_STOPWORDS = {"THE", "AND", "FOR", "TOP", "LONG", "SHORT", "KEY", "NEW"}


def parse_report_tickers(path):
    """Extract plausible tickers from watchlist / top-ideas sections only."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except Exception as e:
        log(f"report read failed {path}: {e}")
        return []
    found, seen, in_section = [], set(), False
    for line in lines:
        if line.startswith("##"):
            in_section = any(k in line.upper() for k in _SECTION_KEYS)
            continue
        if line.startswith("**") and any(
                k in line.upper() for k in _SECTION_KEYS):
            in_section = True
            continue
        if line.startswith("**") and in_section and not any(
                k in line.upper() for k in _SECTION_KEYS):
            # a new bold sub-heading that is not a watched section
            if re.match(r"^\*\*\d+\.", line):
                in_section = False
                continue
        if not in_section:
            continue
        m = _TICKER_LINE.match(line)
        if m:
            t = m.group(1)
            if t not in _STOPWORDS and t not in seen:
                seen.add(t)
                found.append(t)
    return found


def _merge_report_universe(notes):
    """Always-on 7 + today's report names (incl. bench lines), deduped,
    capped at MAX_UNIVERSE — the pre-Nov-3 ad-hoc merge, unchanged.
    Returns (tickers, entries) with each name's source recorded."""
    tickers = list(BASE_TICKERS)
    entries = [{"ticker": t, "source": "always-on"} for t in tickers]
    path = None
    today_name = now_et().strftime("%Y-%m-%d") + ".md"
    cand = os.path.join(REPORTS_DIR, today_name)
    if os.path.exists(cand):
        path = cand
    else:
        try:
            names = sorted(n for n in os.listdir(REPORTS_DIR)
                           if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", n))
            if names:
                path = os.path.join(REPORTS_DIR, names[-1])
        except Exception as e:
            log(f"reports dir scan failed: {e}")
    if path is None:
        notes.append("No Morning Desk report found — universe = always-on only.")
        return tickers, entries
    extra = parse_report_tickers(path)
    if not extra:
        notes.append(f"No tickers parsed from {os.path.basename(path)} — "
                     "universe = always-on only.")
    bench = set()
    try:
        with open(path) as f:
            for _line in f:
                _bm = _TICKER_LINE.match(_line)
                if _bm and "BENCH" in _line.upper():
                    bench.add(_bm.group(1))
    except Exception:
        pass
    for t in extra:
        if t not in tickers:
            tickers.append(t)
            entries.append({"ticker": t, "source":
                            "bench" if t in bench else "report"})
    tickers = tickers[:MAX_UNIVERSE]
    return tickers, entries[:len(tickers)]

# ------------------------------------------------- grouped daily universe
# GROUPED DAILY UNIVERSE + TAPE NAMES (Raj's orders, Nov 3 2026 — "group
# universe everyday first then use that universe for raj clouds everyday"
# and "look at [the morning news tape] but make sure more liquid and very
# less spread stocks only, no junk, no duplicates"): the day's universe is
# assembled ONCE — the first run at/after 09:30 ET builds it (always-on +
# report/bench + filtered tape names, deduped, capped at MAX_UNIVERSE,
# each name's source recorded) and freezes it to UNIVERSE_FILE; every
# later run that day reads the frozen file. Runs before 09:30 ET keep the
# old ad-hoc merge and freeze nothing (premarket option quotes are stale,
# so the tape spread gate is only ever judged on live quotes). Tape names
# are the LOWEST priority and SCAN COVERAGE ONLY (same status as bench):
# no direction, no analysis; any entry still needs every normal gate.
TAPE_DIR = os.path.expanduser("~/workspace/morning-desk/research")
UNIVERSE_FILE = os.path.join(BASE, "raj-intraday-clouds-universe.json")
OPTION_QUOTE_PY = os.path.expanduser(
    "~/workspace/morning-desk/charts/option_quote.py")
TAPE_MIN_PRICE = 10.0
TAPE_MIN_DOLLAR_VOL = 50_000_000.0
TAPE_MAX_SPREAD_PCT = 0.05
_TAPE_TABLE = re.compile(r"^\|\s*([A-Z]{1,5})\s*\|")
_TAPE_BOLD = re.compile(r"^-\s+\*\*[^*]+\*\*\s+([A-Z]{1,5})\b")


def parse_tape_tickers(path):
    """Tickers named in the morning news tape: P1/P2 table first cells
    plus the leading ticker of P3 bold-labelled bullets."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except Exception as e:
        log(f"tape read failed {path}: {e}")
        return []
    found, seen = [], set()
    for line in lines:
        m = _TAPE_TABLE.match(line) or _TAPE_BOLD.match(line)
        if m:
            t = m.group(1)
            if t != "TICKER" and t not in seen:
                seen.add(t)
                found.append(t)
    return found


def _tape_daily_gate(t, today):
    """Gates (a)+(b) on completed daily sessions. Returns
    ((close, avg_dollar_vol), None) or (None, reason) — fail closed."""
    df = yahoo_bars(t, "1d", "3mo")
    if df is None or len(df) == 0:
        return None, "no daily data"
    d = df.copy()
    d.index = d.index.tz_convert(ET)
    d = d[[ix.strftime("%Y-%m-%d") != today for ix in d.index]]
    if len(d) < 20:
        return None, f"only {len(d)} completed sessions"
    close = float(d["Close"].iloc[-1])
    if close < TAPE_MIN_PRICE:
        return None, f"price ${close:.2f} < $10 (junk gate)"
    dvol = float((d["Close"] * d["Volume"]).tail(20).mean())
    if dvol < TAPE_MIN_DOLLAR_VOL:
        return None, f"avg $vol ${dvol / 1e6:.1f}M < $50M (liquidity gate)"
    return (close, dvol), None


def _tape_spread_gate(t, fallback_price):
    """Gate (c): near-ATM front contract via the house option_quote.py
    route (nearest expiry, call side). Real bid required and spread
    <= 5% of mid. Returns (spread_pct, None) or (None, reason)."""
    price = fallback_price
    try:
        m5 = yahoo_bars(t, "5m", "1d")
        if m5 is not None and len(m5) > 0:
            price = float(m5["Close"].dropna().iloc[-1])
    except Exception:
        pass
    try:
        r = subprocess.run(
            [sys.executable, OPTION_QUOTE_PY, t, "LONG", f"{price:.2f}"],
            capture_output=True, text=True, timeout=40)
        if r.returncode != 0:
            return None, "option quote failed"
        q = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as e:
        return None, f"option quote error: {e}"
    bid, ask, mid = q.get("bid"), q.get("ask"), q.get("mid")
    if not bid or bid <= 0 or not ask or not mid or mid <= 0:
        return None, "no real bid (spread unverified)"
    pct = (ask - bid) / mid
    if pct > TAPE_MAX_SPREAD_PCT:
        return None, f"spread {pct * 100:.1f}% of mid > 5%"
    return pct, None


def _build_grouped_universe(notes, today):
    """One daily build: merge (always-on/report/bench) + gated tape
    names by dollar volume until the cap. Returns
    (tickers, entries, tape_added, tape_excluded)."""
    tickers, entries = _merge_report_universe(notes)
    tape_added, tape_excluded = [], []
    tape_path = os.path.join(TAPE_DIR, f"news-tape-{today}.md")
    if not os.path.exists(tape_path):
        notes.append("TAPE NAMES: no tape file for today — grouped "
                     "universe carries no tape names.")
        return tickers, entries, tape_added, tape_excluded
    seen = set(tickers)
    passed = []
    for t in parse_tape_tickers(tape_path):
        if t in seen:
            continue
        res, why = _tape_daily_gate(t, today)
        if res is None:
            tape_excluded.append({"ticker": t, "reason": why})
        else:
            passed.append((t, res[0], res[1]))
    passed.sort(key=lambda x: -x[2])
    for t, close, dvol in passed:
        if len(tickers) >= MAX_UNIVERSE:
            tape_excluded.append(
                {"ticker": t, "reason": "universe cap 25 reached"})
            continue
        pct, why = _tape_spread_gate(t, close)
        if pct is None:
            tape_excluded.append({"ticker": t, "reason": why})
            continue
        tickers.append(t)
        entries.append({"ticker": t, "source": "tape"})
        tape_added.append({"ticker": t, "close": round(close, 2),
                           "avg_dollar_vol": round(dvol),
                           "spread_pct_of_mid": round(pct * 100, 2)})
    if tape_added:
        notes.append("TAPE NAMES added: " + ", ".join(
            a["ticker"] for a in tape_added))
    if tape_excluded:
        notes.append("TAPE NAMES excluded: " + "; ".join(
            f"{e['ticker']} ({e['reason']})" for e in tape_excluded))
    return tickers, entries, tape_added, tape_excluded


def build_universe(notes):
    now = now_et()
    today = now.strftime("%Y-%m-%d")
    try:
        if os.path.exists(UNIVERSE_FILE):
            with open(UNIVERSE_FILE) as f:
                saved = json.load(f)
            if saved.get("date") == today and saved.get("universe"):
                notes.append(
                    f"Grouped universe (built {saved.get('built_at', '?')}"
                    f"): {len(saved['universe'])} names.")
                return list(saved["universe"])
    except Exception as e:
        log(f"universe file read failed: {e}")
    if now.hour * 60 + now.minute < 9 * 60 + 30:
        tickers, _entries = _merge_report_universe(notes)
        return tickers
    tickers, entries, t_added, t_excl = _build_grouped_universe(
        notes, today)
    built_at = now.strftime("%H:%M ET")
    try:
        payload = {"date": today, "built_at": built_at,
                   "universe": tickers, "entries": entries,
                   "tape_added": t_added, "tape_excluded": t_excl}
        tmp = UNIVERSE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=1)
        os.replace(tmp, UNIVERSE_FILE)
        notes.append(f"Grouped universe built for {today} at {built_at}: "
                     f"{len(tickers)} names (sources + tape log in "
                     f"raj-intraday-clouds-universe.json).")
    except Exception as e:
        log(f"universe file write failed: {e}")
    return tickers


# ---------------------------------------------------------------- state file
def load_state(today):
    """Load state; re-init for a new trading day (reset priors + open alerts)."""
    fresh = {"date": today, "initialized": False, "tickers": {},
             "alerts_today": [], "graded_ids": []}
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                st = json.load(f)
            if st.get("date") == today:
                for k, v in fresh.items():
                    st.setdefault(k, v)
                return st, False
    except Exception as e:
        log(f"state read failed: {e}")
    return fresh, True


def save_state(st):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log(f"state write failed: {e}")


def append_audit(rec):
    try:
        with open(AUDIT_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        log(f"audit write failed: {e}")


# ---------------------------------------------------------------- grading
def grade_alerts(st, m5_by_ticker, today):
    """Grade today's ungraded FIRST alerts on today's completed 5m
    bars after entry: +0.50% in direction first = WIN, -0.50% first = LOSS
    (same-bar both-hit counts as LOSS, conservative), else FLAT at close.
    CONFIRMED records are upgrades of a FIRST signal (same entry) and are
    NEVER graded separately — Raj's fix, Oct 28 2026."""
    grades = []
    for rec in st.get("alerts_today", []):
        if rec.get("level") in ("EXIT", "CONFIRMED"):
            continue
        if rec["id"] in st.get("graded_ids", []):
            continue
        df = m5_by_ticker.get(rec["ticker"])
        if df is None or len(df) == 0:
            continue
        entry = float(rec["price"])
        sign = 1 if rec["dir"] == "LONG" else -1
        fav, adv = entry * 1.005, entry * 0.995
        if sign < 0:
            fav, adv = entry * 0.995, entry * 1.005
        bars = df[df.index.strftime("%Y-%m-%d") == today]
        ent = dt.datetime.fromisoformat(rec["entry_time_iso"])
        if ent.tzinfo is None:
            ent = ent.replace(tzinfo=ET)
        bars = bars[bars.index > ent]
        result = "FLAT"
        for _, b in bars.iterrows():
            hi, lo = float(b["High"]), float(b["Low"])
            if sign > 0:
                hit_fav, hit_adv = hi >= fav, lo <= adv
            else:
                hit_fav, hit_adv = lo <= fav, hi >= adv
            if hit_adv:
                result = "LOSS"
                break
            if hit_fav:
                result = "WIN"
                break
        st["graded_ids"].append(rec["id"])
        grades.append({"ticker": rec["ticker"], "dir": rec["dir"],
                       "level": rec["level"], "entry": entry,
                       "entry_time": rec["time"], "result": result})
    return grades


# ---------------------------------------------------------------- main
def main():
    notes, alerts, grades = [], [], []
    now = now_et()
    today = now.strftime("%Y-%m-%d")
    universe = build_universe(notes)
    st, is_new_day = load_state(today)
    initializing = (not st.get("initialized")) or is_new_day

    # SPEED (Raj's order, Oct 29 2026 — "speed up the scanner"): fetch
    # every ticker's bars in parallel up front. The processing loop below
    # is unchanged — same universe order, same logic, same outputs;
    # only the network wait moves off the critical path.
    # CANDIDATE (Nov 2 2026): the daily leg goes through the day cache
    # (provably the same completed frame — see _daily_bars_cached) and
    # the pool is wider; pool.map still preserves universe order and
    # every fetch is independent, so scheduling cannot change outputs.
    _load_daily_cache(today)

    def _fetch(t):
        try:
            core = (_daily_bars_cached(t, today),
                    completed(yahoo_bars(t, "1h", "5d"),
                              dt.timedelta(hours=1)),
                    completed(yahoo_bars(t, "5m", "5d"),
                              dt.timedelta(minutes=5)))
        except Exception as e:
            log(f"prefetch {t} failed: {e}")
            core = (None, None, None)
        # LEVELS instrumentation fetch — isolated on purpose: its failure
        # must never skip a ticker or alter any signal, it only leaves
        # pmh/pml null on that ticker's entry records.
        try:
            prepost = yahoo_bars(t, "5m", "1d", prepost=True)
        except Exception as e:
            log(f"prepost fetch {t} failed: {e}")
            prepost = None
        return core + (prepost,)

    bars_by_ticker = {}
    with ThreadPoolExecutor(max_workers=POOL_WORKERS) as pool:
        for t, bars in zip(universe, pool.map(_fetch, universe)):
            bars_by_ticker[t] = bars
    _save_daily_cache()

    m5_by_ticker = {}
    for t in universe:
        try:
            daily, hourly, m5, prepost = bars_by_ticker[t]
            if m5 is None or len(m5) == 0:
                notes.append(f"{t}: no 5m data — skipped this run.")
                continue
            m5_by_ticker[t] = m5
            states = ticker_states(daily, hourly, m5)
            mc = m5["Close"].dropna()
            last_close = float(mc.iloc[-1])
            ema12 = float(ema(mc, 12).iloc[-1]) if len(mc) >= 12 else None
            bar_time = m5.index[-1]
            bar_time_s = bar_time.strftime("%Y-%m-%d %H:%M ET")
            tstate = st["tickers"].setdefault(
                t, {"prev": None, "open_alert": None})
            prev = tstate.get("prev") or {}
            open_a = tstate.get("open_alert")
            tstate.setdefault("pb_seen", False)

            if not initializing:
                want = {"LONG": "bull", "SHORT": "bear"}
                opp = {"LONG": "bear", "SHORT": "bull"}
                # EXIT first: 1HR cloud flips against the open alert.
                if open_a and states["h1"] == opp[open_a["dir"]]:
                    a = {"ticker": t, "dir": open_a["dir"], "level": "EXIT",
                         "price": round(last_close, 2), "time": bar_time_s,
                         "ema12": round(ema12, 2) if ema12 else None,
                         "entry_price": open_a["entry_price"],
                         "entry_time": open_a["entry_time"],
                         "states": dict(states)}
                    alerts.append(a)
                    append_audit({"ts": now.strftime("%Y-%m-%d %H:%M:%S ET"),
                                  **a})
                    open_a = None
                    tstate["open_alert"] = None
                    tstate["pb_seen"] = False
                    tstate["last_entry"] = None
                # CONFIRMED upgrade: 34/50 cloud agrees with the direction.
                if open_a and open_a.get("level") == "FIRST" \
                        and states["m34_50"] == want[open_a["dir"]]:
                    a = {"ticker": t, "dir": open_a["dir"],
                         "level": "CONFIRMED",
                         "price": round(last_close, 2), "time": bar_time_s,
                         "ema12": round(ema12, 2) if ema12 else None,
                         "entry_price": open_a["entry_price"],
                         "entry_time": open_a["entry_time"],
                         "states": dict(states)}
                    lf = _levels_for(daily, prepost, m5,
                                     a["entry_price"], today,
                                     level="CONFIRMED")
                    a.update(lf)
                    alerts.append(a)
                    append_audit({"ts": now.strftime("%Y-%m-%d %H:%M:%S ET"),
                                  **a})
                    st["alerts_today"].append(
                        {"id": f"{t}-CONFIRMED-{open_a['entry_time']}",
                         "ticker": t, "dir": open_a["dir"],
                         "level": "CONFIRMED", "price": open_a["entry_price"],
                         "time": open_a["entry_time"],
                         "entry_time_iso": open_a["entry_time_iso"], **lf})
                    open_a["level"] = "CONFIRMED"
                # PULLBACK arm: while a leg is open, any 5/12 reading
                # that is not the leg's direction (flat or flipped) means
                # price has pulled back against the trend — arm it.
                if open_a and states["m5_12"] != want[open_a["dir"]]:
                    tstate["pb_seen"] = True
                # PULLBACK entry (Raj's order, Oct 30 2026): the 5/12
                # re-curls INTO the open leg's direction after an armed
                # pullback, with the bigger clouds still healthy — the
                # 1HR cloud agrees with the leg and the daily trend does
                # not oppose. The leg itself stays open (EXIT/CONFIRMED
                # management unchanged); whether a position books is the
                # caller's rule (no position open in ticker+direction).
                if open_a and tstate.get("pb_seen") \
                        and prev.get("m5_12") is not None \
                        and prev.get("m5_12") != want[open_a["dir"]] \
                        and states["m5_12"] == want[open_a["dir"]] \
                        and states["h1"] == want[open_a["dir"]] \
                        and states["dtrend"] != opp[open_a["dir"]] \
                        and ((open_a["dir"] == "LONG"
                              and round(last_close, 2) > float(
                                  tstate.get("last_entry")
                                  or open_a["entry_price"]))
                             or (open_a["dir"] == "SHORT"
                                 and round(last_close, 2) < float(
                                     tstate.get("last_entry")
                                     or open_a["entry_price"]))):
                    blk = _sure_shot_blocked(states, open_a["dir"],
                                             bar_time)
                    if blk is not None:
                        log(f"{t} {open_a['dir']} PULLBACK blocked by "
                            f"after-3PM sure-shot gate ({blk})")
                        notes.append(
                            f"{t}: after-3PM sure-shot gate — "
                            f"{open_a['dir']} PULLBACK logged only, "
                            f"not booked ({blk}).")
                    else:
                        a = {"ticker": t, "dir": open_a["dir"],
                             "level": "PULLBACK",
                             "price": round(last_close, 2),
                             "time": bar_time_s,
                             "ema12": round(ema12, 2) if ema12 else None,
                             "entry_price": round(last_close, 2),
                             "entry_time": bar_time_s,
                             "states": dict(states)}
                        lf = _levels_for(daily, prepost, m5,
                                         a["entry_price"], today,
                                         level="PULLBACK")
                        a.update(lf)
                        alerts.append(a)
                        append_audit({"ts": now.strftime(
                            "%Y-%m-%d %H:%M:%S ET"), **a})
                        st["alerts_today"].append(
                            {"id": f"{t}-PULLBACK-{bar_time_s}",
                             "ticker": t, "dir": open_a["dir"],
                             "level": "PULLBACK",
                             "price": round(last_close, 2),
                             "time": bar_time_s,
                             "entry_time_iso": bar_time.isoformat(),
                             **lf})
                        tstate["pb_seen"] = False
                        tstate["last_entry"] = round(last_close, 2)
                # FIRST: 5/12 cloud flips, daily trend does not oppose.
                if not open_a and prev.get("m5_12") is not None \
                        and prev.get("m5_12") != states["m5_12"]:
                    d = None
                    if states["m5_12"] == "bull" and states["dtrend"] != "bear":
                        d = "LONG"
                    elif states["m5_12"] == "bear" \
                            and states["dtrend"] != "bull":
                        d = "SHORT"
                    if d:
                        blk = _sure_shot_blocked(states, d, bar_time)
                        if blk is not None:
                            log(f"{t} {d} FIRST blocked by after-3PM "
                                f"sure-shot gate ({blk})")
                            notes.append(
                                f"{t}: after-3PM sure-shot gate — {d} "
                                f"FIRST logged only, not booked ({blk}).")
                            d = None
                    if d:
                        a = {"ticker": t, "dir": d, "level": "FIRST",
                             "price": round(last_close, 2), "time": bar_time_s,
                             "ema12": round(ema12, 2) if ema12 else None,
                             "entry_price": round(last_close, 2),
                             "entry_time": bar_time_s,
                             "states": dict(states)}
                        lf = _levels_for(daily, prepost, m5,
                                         a["entry_price"], today,
                                         level="FIRST")
                        a.update(lf)
                        alerts.append(a)
                        append_audit({"ts": now.strftime("%Y-%m-%d %H:%M:%S ET"),
                                      **a})
                        entry_iso = bar_time.isoformat()
                        st["alerts_today"].append(
                            {"id": f"{t}-FIRST-{bar_time_s}", "ticker": t,
                             "dir": d, "level": "FIRST",
                             "price": round(last_close, 2),
                             "time": bar_time_s, "entry_time_iso": entry_iso,
                             **lf})
                        tstate["open_alert"] = {
                            "dir": d, "level": "FIRST",
                            "entry_price": round(last_close, 2),
                            "entry_time": bar_time_s,
                            "entry_time_iso": entry_iso}
                        tstate["pb_seen"] = False
                        tstate["last_entry"] = round(last_close, 2)
            tstate["prev"] = dict(states)
        except Exception as e:
            log(f"{t} failed: {e}")
            notes.append(f"{t}: data error this run — skipped.")

    if initializing:
        st["initialized"] = True
        notes.append("First run of the day — state initialized silently, "
                     "no alerts.")

    if now.hour * 60 + now.minute >= 16 * 60 + 5:
        try:
            grades = grade_alerts(st, m5_by_ticker, today)
        except Exception as e:
            log(f"grading failed: {e}")
            notes.append("Grading failed this run — will retry next run.")

    save_state(st)
    out = {"quiet": not alerts and not grades, "alerts": alerts,
           "grades": grades, "universe": universe, "notes": notes}
    print(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never raise — emit a quiet JSON with the error
        print(json.dumps({"quiet": True, "alerts": [], "grades": [],
                          "universe": BASE_TICKERS,
                          "notes": [f"fatal: {e}"]}))
