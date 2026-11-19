"""
Daily dividend updater for the investment dashboard.

For every fund listed between the AUTO-DIVIDENDS markers in index.html, this:
  1. downloads its recent dividend history from Yahoo Finance (yfinance),
  2. works out how often it pays from the gaps between payment dates,
  3. works out the per-payment amount and the latest ex-dividend date,
  4. rewrites that fund's line in index.html.

Safety rules: if a fund's data can't be fetched, has too few payments, or
the new yearly total looks wildly different from the old one, that fund's
line is left exactly as it was.
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from statistics import median

HTML = "index.html"
START, END = "// AUTO-DIVIDENDS-START", "// AUTO-DIVIDENDS-END"
# Daily amounts only change when Yahoo's rough estimate differs by more than this (a real rate change).
DAILY_CHANGE_THRESHOLD = 0.25
LINE_RE = re.compile(
    r"^\s*(?P<q>'?)(?P<sym>[A-Z0-9.\-]+)(?P=q):\s*\{\s*perShare:\s*(?P<per>[\d.]+),\s*frequency:\s*(?P<freq>\d+),"
    r"\s*freqLabel:\s*'(?P<label>[^']*)',\s*exDay:\s*\{month:\s*(?P<m>\d+),\s*day:\s*(?P<d>\d+)\}\s*\},?\s*$"
)

# (max median gap in days, payments per year, label)
CADENCES = [
    (4, 365, "Daily"),
    (10, 52, "Weekly"),
    (15, 26, "Bi-Weekly"),
    (20, 24, "Semi-Monthly"),
    (45, 12, "Monthly"),
    (135, 4, "Quarterly"),
    (250, 2, "Semi-Annual"),
    (10**6, 1, "Annual"),
]


def cadence_from_dates(dates):
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    gaps = [g for g in gaps if g > 0][-3:]  # last 3 gaps only, so a schedule change shows up within a few payments
    if not gaps:
        return None
    g = median(gaps)
    for max_gap, freq, label in CADENCES:
        if g <= max_gap:
            return freq, label, max_gap
    return None


def compute(series, now):
    """series: list of (datetime, amount) sorted by date. Returns dict or None."""
    recent = [(d, a) for d, a in series if d >= now - timedelta(days=400) and a > 0]
    if len(recent) < 2:
        return None
    dates = [d for d, _ in recent]
    cad = cadence_from_dates(dates)
    if not cad:
        return None
    freq, label, max_gap = cad
    last_date, last_amt = recent[-1]
    if (now - last_date).days > max(3 * 365 / freq, 10):
        return None  # stale: fund seems to have stopped paying; leave as-is
    if freq >= 52:
        # Frequent payers: amounts per payment wobble (e.g. a Monday payment can cover a weekend),
        # so use the average daily rate over the last ~28 days instead. Only count payments made
        # under the current schedule (stop at the first gap that's too long for it).
        start = len(recent) - 1
        while start > 0 and (recent[start][0] - recent[start - 1][0]).days <= max_gap \
                and (last_date - recent[start - 1][0]).days <= 28:
            start -= 1
        run = recent[start:]
        days = (run[-1][0] - run[0][0]).days
        if len(run) < 3 or days <= 0:
            return None
        if freq == 365:
            # Daily payers: Yahoo's record is patchy (missing days, several days lumped together), so
            # this is only a rough estimate: total paid / calendar days covered. update() only uses it
            # when it differs a lot from the current amount (i.e. a real rate change).
            per_share = sum(a for _, a in run[1:]) / days
        else:
            # each payment covers the days since the previous one, so skip the first payment's amount
            per_share = sum(a for _, a in run[1:]) / days * 365 / freq
    else:
        per_share = last_amt
    return {"perShare": per_share, "frequency": freq, "freqLabel": label,
            "month": last_date.month - 1, "day": last_date.day}  # JS months are 0-based


SYMBOL_OK = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")


def fetch_holding_symbols():
    """Symbols currently in the dashboard's Supabase holdings table (empty list if not configured)."""
    url, key = os.environ.get("SUPABASE_URL", "").rstrip("/"), os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        print("Supabase not configured; only updating funds already on the dividend list")
        return []
    headers = {"apikey": key}
    if not key.startswith("sb_"):          # older-style keys are also sent as a bearer token
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(f"{url}/rest/v1/holdings?select=symbol", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            rows = json.load(r)
    except Exception as e:
        print(f"Could not read holdings from Supabase ({e}); only updating funds already on the list")
        return []
    syms = sorted({str(row.get("symbol", "")).strip().upper() for row in rows})
    return [x for x in syms if SYMBOL_OK.match(x)]


def fetch(sym):
    import yfinance as yf
    s = yf.Ticker(sym.replace(".", "-")).dividends   # Yahoo writes BRK.B as BRK-B
    out = []
    for idx, amt in s.items():
        d = idx.to_pydatetime()
        d = d.replace(tzinfo=None)
        out.append((d, float(amt)))
    return sorted(out)


def js_key(sym):
    return sym if re.match(r"^[A-Z_$][A-Z0-9_$]*$", sym) else f"'{sym}'"


def fmt_line(sym, v):
    per = f"{v['perShare']:.4f}".rstrip("0").rstrip(".") if v["perShare"] < 1 else f"{v['perShare']:.2f}"
    return (f"  {js_key(sym) + ':':<6}{{ perShare: {per + ',':<8}frequency: {str(v['frequency']) + ',':<4} "
            f"freqLabel: {repr(v['freqLabel']) + ',':<15}exDay: {{month: {v['month']}, day: {v['day']}}} }},")


def update(html, fetcher, now, held=()):
    a, b = html.index(START) + len(START), html.index(END)
    lines = html[a:b].strip("\n").split("\n")
    new_lines, changes = [], []
    for line in lines:
        m = LINE_RE.match(line)
        if not m:
            new_lines.append(line)
            continue
        sym = m["sym"]
        old = {"perShare": float(m["per"]), "frequency": int(m["freq"]), "freqLabel": m["label"],
               "month": int(m["m"]), "day": int(m["d"])}
        try:
            new = compute(fetcher(sym), now)
        except Exception as e:  # network / data problems: keep old values
            print(f"{sym}: could not fetch ({e}); keeping existing values")
            new_lines.append(line)
            continue
        if new is None:
            print(f"{sym}: not enough recent data; keeping existing values")
            new_lines.append(line)
            continue
        old_annual = old["perShare"] * old["frequency"]
        if new["frequency"] == 365 and old_annual > 0:
            if old["frequency"] != 365:
                # just switched to daily: payers normally keep the same yearly total, so carry it over
                new["perShare"] = old_annual / 365
                print(f"{sym}: switched to daily; keeping the same yearly total ({old_annual:.2f}/share)")
            elif abs(new["perShare"] / old["perShare"] - 1) < DAILY_CHANGE_THRESHOLD:
                # within the normal noise of Yahoo's daily data: keep the current amount
                new["perShare"] = old["perShare"]
            else:
                print(f"{sym}: daily amount looks different ({old['perShare']:.4f} -> {new['perShare']:.4f}); updating")
        new_annual = new["perShare"] * new["frequency"]
        if old_annual > 0 and not (1 / 3 <= new_annual / old_annual <= 3):
            print(f"{sym}: yearly total jumped from {old_annual:.2f} to {new_annual:.2f}; looks suspicious, keeping existing values")
            new_lines.append(line)
            continue
        line_new = fmt_line(sym, new)
        if old["freqLabel"] != new["freqLabel"]:
            changes.append(f"{sym}: {old['freqLabel']} -> {new['freqLabel']}")
        new_lines.append(line_new)
        print(f"{sym}: {new['freqLabel']} ({new['frequency']}x/yr), {new['perShare']:.4f}/share, last ex-date month={new['month']+1} day={new['day']}")
    # Funds in your holdings that aren't on the dividend list yet: look them up and add them.
    listed = {m["sym"] for m in (LINE_RE.match(l) for l in new_lines) if m}
    for sym in held:
        if sym in listed:
            continue
        try:
            new = compute(fetcher(sym), now)
        except Exception as e:
            print(f"{sym} (new holding): could not fetch ({e}); will try again tomorrow")
            continue
        if new is None:
            print(f"{sym} (new holding): no recent dividends found (it may not pay any); not added")
            continue
        new_lines.append(fmt_line(sym, new))
        changes.append(f"{sym}: added ({new['freqLabel']})")
        print(f"{sym} (new holding): added - {new['freqLabel']} ({new['frequency']}x/yr), {new['perShare']:.4f}/share")
    out = html[:a] + "\n" + "\n".join(new_lines) + "\n" + html[b:]
    return out, changes


def main():
    html = open(HTML, encoding="utf-8").read()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    out, changes = update(html, fetch, now, fetch_holding_symbols())
    if changes:
        print("Changes:", "; ".join(changes))
    if out != html:
        open(HTML, "w", encoding="utf-8").write(out)
        print("index.html updated")
    else:
        print("No changes")


if __name__ == "__main__":
    sys.exit(main())
