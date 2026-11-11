#!/usr/bin/env python3
"""chains.py — warm option-chain cache for the Raj Intraday Clouds watcher.

Part of the Clouds latency package Raj approved Nov 5 2026 ("Build the
watcher + warm chains"). SHADOW artifact: this module only writes
chains-cache.json inside clouds-watcher/. Nothing live reads it yet.

Purpose: the Entries worker currently makes two live Nasdaq chain pulls per
booking (verify + reproduce). With this cache warm, the proposed live wiring
is: first pull = local cache read (if the ticker's entry is < 90 s old),
second pull = one live confirm. See README.md.

Source: Nasdaq's public option-chain API — the exact endpoint and row shape
~/workspace/morning-desk/charts/option_quote.py uses (stocks first, etf
fallback for SPY/QQQ/IWM). No key. Stdlib only.

Trim: front TWO expiries (>= today), strikes within +/-7% of the underlying
price printed on the chain ("LAST TRADE: $..."). Rows carry bid/ask for both
sides plus last/volume/open-interest when Nasdaq provides them.

Failure handling: per-ticker failures are silent — the ticker's last good
cache entry is kept (its fetched_at stays old, so staleness is visible). A
HTTP 429 stops the whole pass immediately (provider hard stop); tickers not
yet refreshed keep their previous entries.

Usage:
    chains.py --refresh-once   one refresh pass over the frozen universe
    chains.py --lookup TICKER CALL|PUT STRIKE EXPIRY(YYYY-MM-DD)
    chains.py --self-test
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
WATCH_DIR = Path(__file__).resolve().parent
DESK = WATCH_DIR.parent
UNIVERSE_FILE = DESK / "raj-intraday-clouds-universe.json"
CACHE_FILE = WATCH_DIR / "chains-cache.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
API = "https://api.nasdaq.com/api/quote/{t}/option-chain?assetclass=stocks&limit=2000"
API_ETF = "https://api.nasdaq.com/api/quote/{t}/option-chain?assetclass=etf&limit=2000"

BASE_TICKERS = ["SPY", "QQQ", "IWM", "TSLA", "NVDA", "MSFT", "AMZN"]
FRONT_EXPIRIES = 2
TRIM_PCT = 0.07          # strikes within +/-7% of spot
FETCH_GAP_S = 0.4        # sequential pulls, small gap (provider courtesy)
FRESH_S = 90             # proposed live wiring: cache usable as pull #1 if younger


class RateLimited(Exception):
    """HTTP 429 from Nasdaq — hard stop for the pass."""


# ---------------------------------------------------------------- helpers
def fnum(v):
    """Nasdaq quote strings ('--' / null / '1.23') -> float or None.
    Same parser as option_quote.py."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s in ("", "--", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(f"429 on {url}")
        raise


def fetch_chain(ticker):
    """One ticker's raw Nasdaq payload; stocks endpoint, etf fallback —
    identical to option_quote.py's chain selection."""
    payload = fetch_json(API.format(t=ticker))
    data = payload.get("data")
    if not data or not data.get("table", {}).get("rows"):
        payload = fetch_json(API_ETF.format(t=ticker))
    return payload


def load_universe():
    """Tickers from the FROZEN universe file (read-only — the watcher never
    writes it). Fallback: the always-on 7, flagged in the return."""
    try:
        saved = json.loads(UNIVERSE_FILE.read_text())
        tickers = [str(t).upper() for t in saved.get("universe", [])]
        if tickers:
            return tickers, f"frozen file ({saved.get('date')})"
    except Exception:
        pass
    return list(BASE_TICKERS), "fallback always-on 7 (universe file unreadable)"


# ---------------------------------------------------------------- parsing (pure)
def parse_chain(ticker, payload, today):
    """Raw Nasdaq payload -> trimmed cache entry (pure; unit-tested).

    Returns {ticker, underlying, expiries: [{expiry, rows: [{strike,
    call: {bid, ask, last}, put: {...}, volume, open_interest}]}]}
    or None when the payload carries no usable chain.
    """
    data = (payload or {}).get("data")
    if not data or not data.get("table", {}).get("rows"):
        return None

    # Group rows by expiry exactly like option_quote.py: a header row
    # (strike None, expirygroup set) precedes its data rows.
    groups, order, current = {}, [], None
    for r in data["table"]["rows"]:
        g = (r.get("expirygroup") or "").strip()
        if r.get("strike") is None:
            if g:
                current = g
                if g not in groups:
                    groups[g] = []
                    order.append(g)
            continue
        if current and current in groups:
            groups[current].append(r)
    if not order:
        return None

    underlying = None
    m = re.search(r"\$([\d,]+\.?\d*)", data.get("lastTrade") or "")
    if m:
        underlying = float(m.group(1).replace(",", ""))

    dated = []
    for g in order:
        try:
            d = datetime.strptime(g, "%B %d, %Y").date()
        except ValueError:
            continue
        if d >= today and groups[g]:
            dated.append((d, g))
    dated.sort()
    dated = dated[:FRONT_EXPIRIES]
    if not dated:
        return None

    expiries = []
    for d, g in dated:
        rows = []
        for r in groups[g]:
            strike = fnum(r.get("strike"))
            if strike is None:
                continue
            if underlying is not None:
                if not (underlying * (1 - TRIM_PCT) <= strike
                        <= underlying * (1 + TRIM_PCT)):
                    continue
            rows.append({
                "strike": strike,
                "call": {"bid": fnum(r.get("c_Bid")), "ask": fnum(r.get("c_Ask")),
                         "last": fnum(r.get("c_Last"))},
                "put": {"bid": fnum(r.get("p_Bid")), "ask": fnum(r.get("p_Ask")),
                        "last": fnum(r.get("p_Last"))},
                "volume": fnum(r.get("c_Volume")),
                "open_interest": fnum(r.get("c_Openinterest")),
            })
        rows.sort(key=lambda x: x["strike"])
        if rows:
            expiries.append({"expiry": d.isoformat(), "rows": rows})
    if not expiries:
        return None
    return {"ticker": ticker, "underlying": underlying, "expiries": expiries}


# ---------------------------------------------------------------- cache io
def load_cache():
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {"tickers": {}}


def save_cache(cache):
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, indent=1) + "\n")
    tmp.replace(CACHE_FILE)


def lookup(ticker, opt_type, strike, expiry, cache=None, now=None):
    """Local read for the proposed live wiring. Returns (row, age_s) or
    (None, None). opt_type: 'call' | 'put'. A row is only handed back with
    its age — the caller decides freshness (proposed bar: < FRESH_S)."""
    cache = cache if cache is not None else load_cache()
    ent = (cache.get("tickers") or {}).get(ticker.upper())
    if not ent or ent.get("fetched_at_epoch") is None:
        return None, None
    now = now or datetime.now(ET)
    age = now.timestamp() - ent["fetched_at_epoch"]
    for exp in ent.get("expiries", []):
        if exp["expiry"] != expiry:
            continue
        for row in exp["rows"]:
            if abs(row["strike"] - float(strike)) < 1e-9:
                return {**row[opt_type], "strike": row["strike"],
                        "expiry": expiry, "underlying": ent.get("underlying")}, age
    return None, age


# ---------------------------------------------------------------- refresh
def refresh_once():
    """One refresh pass over the frozen universe. Returns a summary dict."""
    now = datetime.now(ET)
    tickers, src = load_universe()
    cache = load_cache()
    cache.setdefault("tickers", {})
    ok, failed, rate_limited = [], [], False
    for i, tk in enumerate(tickers):
        if i:
            time.sleep(FETCH_GAP_S)
        try:
            payload = fetch_chain(tk)
            entry = parse_chain(tk, payload, now.date())
            if entry is None:
                raise ValueError("no usable chain in payload")
            entry["fetched_at"] = now.strftime("%Y-%m-%d %H:%M:%S ET")
            entry["fetched_at_epoch"] = now.timestamp()
            cache["tickers"][tk] = entry
            ok.append(tk)
        except RateLimited:
            rate_limited = True
            failed.append(tk)
            break  # provider hard stop — remaining tickers keep last good
        except Exception as e:
            failed.append(tk)
            old = cache["tickers"].get(tk)
            if old is not None:
                old["last_error"] = f"{type(e).__name__}: {e}"
            # last good entry (if any) stays untouched otherwise
    cache["refreshed_at"] = now.strftime("%Y-%m-%d %H:%M:%S ET")
    cache["universe_source"] = src
    cache["rate_limited"] = rate_limited
    save_cache(cache)
    return {"refreshed_at": cache["refreshed_at"], "universe_source": src,
            "ok": ok, "failed": failed, "rate_limited": rate_limited,
            "tickers_cached": len(cache["tickers"])}


# ---------------------------------------------------------------- self-test
def _fake_payload():
    def row(strike, cb, ca, pb, pa):
        return {"strike": str(strike), "c_Bid": str(cb), "c_Ask": str(ca),
                "c_Last": str(ca), "p_Bid": str(pb), "p_Ask": str(pa),
                "c_Volume": "10", "c_Openinterest": "100", "expirygroup": ""}
    rows = [{"strike": None, "expirygroup": "November 4, 2026"}]  # past — dropped
    rows.append(row(100, 1.0, 1.1, 1.0, 1.1))
    rows.append({"strike": None, "expirygroup": "November 6, 2026"})
    for s in range(85, 116, 5):
        rows.append(row(s, 1.0, 1.1, 0.9, 1.0))
    rows.append({"strike": None, "expirygroup": "November 13, 2026"})
    for s in range(90, 111, 5):
        rows.append(row(s, 2.0, 2.1, 1.9, 2.0))
    rows.append({"strike": None, "expirygroup": "November 20, 2026"})  # 3rd — dropped
    rows.append(row(100, 3.0, 3.1, 2.9, 3.0))
    return {"data": {"lastTrade": "LAST TRADE: $100.00 (AS OF NOV 5, 2026)",
                     "table": {"rows": rows}}}


def self_test():
    from datetime import date
    fails = 0

    def check(name, good, got):
        nonlocal fails
        print(f"{'PASS' if good else 'FAIL'}  {name}: {got}")
        fails += 0 if good else 1

    ent = parse_chain("TEST", _fake_payload(), date(2026, 11, 5))
    exps = [e["expiry"] for e in ent["expiries"]] if ent else []
    check("front two expiries only, past dropped",
          exps == ["2026-11-06", "2026-11-13"], exps)
    strikes = [r["strike"] for r in ent["expiries"][0]["rows"]] if ent else []
    check("trim to +/-7% of spot 100 (93..107)",
          strikes == [95.0, 100.0, 105.0], strikes)
    check("underlying parsed from lastTrade",
          ent is not None and ent["underlying"] == 100.0,
          ent["underlying"] if ent else None)
    cache = {"tickers": {"TEST": {**ent,
                                  "fetched_at_epoch": datetime(2026, 11, 5, 12, 0, 0,
                                                               tzinfo=ET).timestamp()}}}
    row, age = lookup("TEST", "put", 100, "2026-11-06", cache=cache,
                      now=datetime(2026, 11, 5, 12, 0, 45, tzinfo=ET))
    check("lookup returns exact row + age",
          row is not None and row["bid"] == 0.9 and age == 45.0,
          f"row={row} age={age}")
    row2, _ = lookup("TEST", "call", 85, "2026-11-06", cache=cache,
                     now=datetime(2026, 11, 5, 12, 0, 45, tzinfo=ET))
    check("lookup misses trimmed strike", row2 is None, row2)
    print(f"chains self-test: {5 - fails}/5 passed")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-once", action="store_true")
    ap.add_argument("--lookup", nargs=4, metavar=("TICKER", "TYPE", "STRIKE", "EXPIRY"))
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.lookup:
        tk, typ, strike, expiry = a.lookup
        row, age = lookup(tk, typ.lower(), float(strike), expiry)
        print(json.dumps({"row": row, "age_s": age,
                          "fresh": (age is not None and age < FRESH_S)}))
        return 0
    if a.refresh_once:
        t0 = time.time()
        out = refresh_once()
        out["elapsed_s"] = round(time.time() - t0, 2)
        print(json.dumps(out, indent=1))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
