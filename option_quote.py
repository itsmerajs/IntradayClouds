#!/usr/bin/env python3
"""Live US equity option quote + BEST-CONTRACT PICK via Nasdaq's public
option-chain API (no key).

Usage: python3 option_quote.py TICKER LONG|SHORT ENTRY_PRICE [TARGET_PRICE]
  Expiry: same-day (0DTE) if listed, else the nearest listed expiry.
  Strike PICK (Raj's rule, Oct 12 2026): evaluate the 5 strikes nearest to
  ENTRY_PRICE (call for LONG, put for SHORT) on the LIVE chain and pick the
  one with the best option_R:
      option_R = (target_premium_est - entry_premium) / entry_premium
  entry_premium = ask (what Raj would pay; mid if ask missing).
  target_premium_est = intrinsic value at TARGET_PRICE
                     + residual extrinsic (25% of current extrinsic, floored 0).
  Skip a strike when bid/ask are missing or the spread exceeds 60% of mid
  (illiquid). If TARGET_PRICE is omitted, fall back to nearest-strike-to-entry
  with no R computation (legacy behavior).

  Prints one JSON line to stdout:
    {"contract_symbol","expiry":"YYYY-MM-DD","strike","type":"call|put",
     "bid","ask","mid","underlying","quote_time",
     "entry_premium","target_premium_est","option_R",
     "candidates":[{"strike","bid","ask","mid","option_R","skipped"}]}
  Numbers are floats; null where unavailable. Never fabricates data.
  On failure: exit non-zero, reason on stderr.
"""
import datetime
import json
import os
import re
import sys
import urllib.request
from zoneinfo import ZoneInfo

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
API = "https://api.nasdaq.com/api/quote/{t}/option-chain?assetclass=stocks&limit=2000"
API_ETF = "https://api.nasdaq.com/api/quote/{t}/option-chain?assetclass=etf&limit=2000"
ET = ZoneInfo("America/New_York")

# DATA SOURCE — switched Fri Nov 20 2026 on Raj's explicit order: chain
# and quote data now come from the shared Tradier data layer
# (~/workspace/shared/tradier-data/marketdata.py) — Tradier primary,
# Nasdaq is the layer's own fallback. The strike-pick logic below is
# unchanged; only where the chain comes from has moved.
sys.path.insert(0, os.path.expanduser("~/workspace/shared/tradier-data"))
import marketdata as _md
MAX_SPREAD_PCT = 0.60
RESIDUAL_EXTRINSIC_FRAC = 0.25
N_CANDIDATES = 5


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.load(r)
    except Exception as e:
        die(f"request failed: {e}")


def fnum(v):
    """Parse Nasdaq's quote strings ('--' / null / '1.23') to float or None."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s in ("", "--", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def occ_symbol(ticker, expiry_ymd, is_call, strike):
    # OCC 21-char symbol: TICKER(6, space-padded) + YYMMDD + C/P + strike*1000 (8 digits)
    yymmdd = expiry_ymd[2:4] + expiry_ymd[5:7] + expiry_ymd[8:10]
    return f"{ticker.upper()[:6].ljust(6)}{yymmdd}{'C' if is_call else 'P'}{int(round(strike * 1000)):08d}"


def intrinsic(is_call, strike, price):
    return max(price - strike, 0.0) if is_call else max(strike - price, 0.0)


def main():
    if len(sys.argv) not in (4, 5):
        die("usage: option_quote.py TICKER LONG|SHORT ENTRY_PRICE [TARGET_PRICE]")
    ticker = sys.argv[1].upper().strip()
    direction = sys.argv[2].upper().strip()
    if direction not in ("LONG", "SHORT"):
        die("direction must be LONG or SHORT")
    try:
        entry = float(sys.argv[3])
    except ValueError:
        die("ENTRY_PRICE must be a number")
    target = None
    if len(sys.argv) == 5:
        try:
            target = float(sys.argv[4])
        except ValueError:
            die("TARGET_PRICE must be a number")
    is_call = direction == "LONG"

    # Chain via the shared Tradier data layer (Tradier primary, Nasdaq
    # fallback inside the layer). Expiry pick unchanged: same-day (0DTE)
    # if listed, else the nearest listed expiry.
    today = datetime.datetime.now(ET).date()
    dated = []
    for dstr in _md.expirations(ticker):
        try:
            d = datetime.date.fromisoformat(dstr)
        except ValueError:
            continue
        if d >= today:
            dated.append(d)
    if not dated:
        die(f"no current/future expiries listed for {ticker}")
    dated.sort()
    exp_date = today if today in dated else dated[0]

    rows = _md.chain(ticker, exp_date.isoformat())
    if not rows:
        die(f"no option chain data returned for {ticker}")
    strikes = sorted({r.get("strike") for r in rows
                      if r.get("strike") is not None})
    if not strikes:
        die(f"no strikes listed for {ticker} {exp_date.isoformat()}")

    # Underlying price from the layer's quote; quote_time is the
    # chain's own pull time (the layer's asof stamp).
    q = _md.quotes([ticker]).get(ticker) or {}
    underlying = q.get("last")
    quote_time = rows[0].get("asof")

    ref = underlying if underlying else entry
    side = "call" if is_call else "put"
    by_strike = {}
    for r in rows:
        if r.get("type") == side and r.get("strike") is not None:
            by_strike.setdefault(r["strike"], r)
    nearest = sorted(strikes, key=lambda s: (abs(s - ref), s))[:N_CANDIDATES]

    def quote_row(strike):
        row = by_strike.get(strike) or {}
        bid, ask = row.get("bid"), row.get("ask")
        mid = round((bid + ask) / 2, 4) if bid is not None and ask is not None else None
        return bid, ask, mid

    candidates = []
    for s in nearest:
        bid, ask, mid = quote_row(s)
        c = {"strike": float(s), "bid": bid, "ask": ask, "mid": mid,
             "option_R": None, "skipped": None}
        if target is None:
            candidates.append(c)
            continue
        if bid is None or ask is None or ask <= 0:
            c["skipped"] = "no two-sided quote"
            candidates.append(c)
            continue
        if mid and mid > 0 and (ask - bid) / mid > MAX_SPREAD_PCT:
            c["skipped"] = "spread too wide"
            candidates.append(c)
            continue
        entry_premium = ask  # what Raj would actually pay
        now_intr = intrinsic(is_call, s, ref)
        extrinsic = max(mid - now_intr, 0.0)
        tgt_premium = intrinsic(is_call, s, target) + RESIDUAL_EXTRINSIC_FRAC * extrinsic
        r = (tgt_premium - entry_premium) / entry_premium
        c["option_R"] = round(r, 2)
        c["entry_premium"] = round(entry_premium, 4)
        c["target_premium_est"] = round(tgt_premium, 4)
        candidates.append(c)

    if target is None:
        # Legacy: nearest strike to entry, no R.
        strike = min(strikes, key=lambda s: (abs(s - entry), s))
        bid, ask, mid = quote_row(strike)
        pick = {"strike": float(strike), "bid": bid, "ask": ask, "mid": mid}
    else:
        viable = [c for c in candidates if c["option_R"] is not None]
        if not viable:
            die(f"no liquid candidate strikes for {ticker} {exp_date.isoformat()} "
                f"(all skipped: {[ (c['strike'], c['skipped']) for c in candidates ]})")
        pick = max(viable, key=lambda c: c["option_R"])

    strike = pick["strike"]
    bid, ask, mid = pick["bid"], pick["ask"], pick["mid"]

    out = {
        "contract_symbol": occ_symbol(ticker, exp_date.isoformat(), is_call, strike),
        "expiry": exp_date.isoformat(),
        "strike": float(strike),
        "type": "call" if is_call else "put",
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "underlying": underlying,
        "quote_time": quote_time,
        "entry_premium": pick.get("entry_premium"),
        "target_premium_est": pick.get("target_premium_est"),
        "option_R": pick.get("option_R"),
        "candidates": candidates,
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
