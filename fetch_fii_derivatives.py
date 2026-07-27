"""
Runs on GitHub Actions (see .github/workflows/fii-derivatives.yml),
completely independent of fetch_bhavcopy.py -- separate script, separate
workflow, separate data. Fetches two NSE derivatives reports and stores
them raw in the shared data repo under FII-FPI/:

  1. F&O - FII Derivatives Statistics
     https://nsearchives.nseindia.com/content/fo/fii_stats_{DD-Mon-YYYY}.xls
     -> FII-FPI/fii-derivatives-stats/fii_derivatives_stats_{YYYY-MM-DD}.xls
     Binary (.xls). FII-only: buy/sell contracts + value, and end-of-day
     OI, broken down per index and per instrument type.

  2. F&O - Participant wise Open Interest
     https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{DDMMYYYY}.csv
     -> FII-FPI/participant-wise-oi/participant_wise_oi_{YYYY-MM-DD}.csv
     Text (.csv). All 4 participant types (Client/DII/FII/Pro), OI only,
     split into Long vs Short per instrument type.

Neither report needs the cookie/session handshake fetch_fii_dii.py
requires for NSE's fiidiiTradeReact endpoint -- both archive URLs are
fetched directly, same as fetch_bhavcopy.py's own NSE_URL.

Idempotent by design, same as fetch_bhavcopy.py: re-running for a date
that's already cached in the data repo just skips straight through.

Three modes, selected by which env vars are set (mirrors fetch_bhavcopy.py's
TARGET_DATE override, extended with a range for backfill):

  1. Scheduled / no override  -> fetch today (IST) only.
  2. TARGET_DATE set          -> fetch that single specific date.
  3. START_DATE + END_DATE set -> backfill mode: loop every weekday in the
     range (inclusive), fetching both reports for each. A 404 on any given
     day is logged as "not available" and the loop continues -- this is
     expected for holidays that aren't weekends, and for dates before
     either report existed. Safe to re-run or resume at any point, since
     each day's fetch is independently idempotent.
"""
import base64
import os
import sys
from datetime import date, datetime, timedelta, timezone

import requests

# ---- Config -------------------------------------------------------------
DATA_REPO = os.environ.get("DATA_REPO", "amol-bendre/stock-market-data")
TOKEN = os.environ["DATA_REPO_TOKEN"]  # GitHub Actions secret, write access to DATA_REPO
IST = timezone(timedelta(hours=5, minutes=30))

FII_STATS_URL = "https://nsearchives.nseindia.com/content/fo/fii_stats_{dmy}.xls"
PARTICIPANT_OI_URL = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{ddmmyyyy}.csv"

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}

GH_API = "https://api.github.com"
GH_HEADERS = {
    "Authorization": f"token {TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

FII_STATS_MIN_BYTES = 2000  # a real file is ~9 KB; a stub/error page would be far smaller


def today_ist() -> date:
    return datetime.now(IST).date()


def parse_env_date(name):
    v = os.environ.get(name, "").strip()
    return date.fromisoformat(v) if v else None


# ---- GitHub Contents API helpers (binary-safe) --------------------------
def gh_get_sha(path):
    """Returns the file's current sha if it exists, else None. Used both
    for idempotency checks (existence only, no need to download content)
    and as the required sha when overwriting."""
    r = requests.get(f"{GH_API}/repos/{DATA_REPO}/contents/{path}", headers=GH_HEADERS, timeout=15)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json().get("sha")


def gh_put_bytes(path, content_bytes, message, sha=None):
    """Binary-safe push -- used for both files here, since fii_stats.xls
    is genuine binary and treating participant_oi.csv the same way avoids
    any encoding guesswork on NSE's own charset for that file."""
    payload = {"message": message, "content": base64.b64encode(content_bytes).decode()}
    if sha:
        payload["sha"] = sha
    r = requests.put(f"{GH_API}/repos/{DATA_REPO}/contents/{path}", headers=GH_HEADERS, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


# ---- NSE fetch ------------------------------------------------------------
def fetch_fii_stats(d: date):
    """Returns raw xls bytes, or None if this was a non-trading day / not
    yet published (clean 404)."""
    url = FII_STATS_URL.format(dmy=d.strftime("%d-%b-%Y"))
    r = requests.get(url, headers=NSE_HEADERS, timeout=20)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    if len(r.content) < FII_STATS_MIN_BYTES:
        raise ValueError(
            f"fii_stats file for {d} looks too small ({len(r.content)} bytes) -- "
            f"possibly an error page rather than a real xls"
        )
    return r.content


def fetch_participant_oi(d: date):
    """Returns raw csv bytes, or None if this was a non-trading day / not
    yet published (clean 404)."""
    url = PARTICIPANT_OI_URL.format(ddmmyyyy=d.strftime("%d%m%Y"))
    r = requests.get(url, headers=NSE_HEADERS, timeout=20)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    if b"Client Type" not in r.content:
        raise ValueError(f"participant_oi file for {d} missing expected 'Client Type' header row -- possibly corrupted")
    return r.content


# ---- Ensure-cached wrappers, one per report ------------------------------
def ensure_fii_stats_cached(d: date):
    path = f"FII-FPI/fii-derivatives-stats/fii_derivatives_stats_{d}.xls"
    if gh_get_sha(path) is not None:
        print(f"[fii_stats] {path} already cached, skipping fetch")
        return True

    print(f"[fii_stats] fetching NSE FII derivatives stats for {d} ...")
    content = fetch_fii_stats(d)
    if content is None:
        print(f"[fii_stats] NSE returned 404 for {d} -- not a trading day (or not yet published)")
        return False

    gh_put_bytes(path, content, f"FII derivatives stats {d}")
    print(f"[fii_stats] pushed {path} ({len(content):,} bytes)")
    return True


def ensure_participant_oi_cached(d: date):
    path = f"FII-FPI/participant-wise-oi/participant_wise_oi_{d}.csv"
    if gh_get_sha(path) is not None:
        print(f"[participant_oi] {path} already cached, skipping fetch")
        return True

    print(f"[participant_oi] fetching NSE participant-wise OI for {d} ...")
    content = fetch_participant_oi(d)
    if content is None:
        print(f"[participant_oi] NSE returned 404 for {d} -- not a trading day (or not yet published)")
        return False

    gh_put_bytes(path, content, f"Participant-wise OI {d}")
    print(f"[participant_oi] pushed {path} ({len(content):,} bytes)")
    return True


# ---- Orchestration --------------------------------------------------------
def run_single(d: date):
    print(f"=== FII reports job for {d} (IST) ===")
    ok_fii_stats = ensure_fii_stats_cached(d)
    ok_participant_oi = ensure_participant_oi_cached(d)
    return ok_fii_stats, ok_participant_oi


def run_backfill(start: date, end: date):
    print(f"=== FII reports backfill: {start} -> {end} (weekdays only) ===")
    results = []
    d = start
    while d <= end:
        if d.weekday() >= 5:  # Saturday=5, Sunday=6
            print(f"[backfill] {d} is a weekend -- skipping")
        else:
            ok_fii_stats, ok_participant_oi = run_single(d)
            results.append((d, ok_fii_stats, ok_participant_oi))
        d += timedelta(days=1)

    total = len(results)
    both_ok = sum(1 for _, a, b in results if a and b)
    print(f"=== Backfill complete: {both_ok}/{total} weekdays fully fetched ===")
    incomplete = [(d, a, b) for d, a, b in results if not (a and b)]
    if incomplete:
        print(f"--- {len(incomplete)} day(s) with at least one missing report: ---")
        for d, a, b in incomplete:
            print(f"  {d}: fii_stats={'OK' if a else 'MISSING'}  participant_oi={'OK' if b else 'MISSING'}")


def main():
    start = parse_env_date("START_DATE")
    end = parse_env_date("END_DATE")

    if start and end:
        run_backfill(start, end)
        return  # Backfill is always manual (workflow_dispatch); IS_LAST_ATTEMPT
                 # will naturally be false since github.event.schedule is unset
                 # for manual runs, so no Telegram alert fires from this mode.

    override = os.environ.get("TARGET_DATE", "").strip()
    d = date.fromisoformat(override) if override else today_ist()

    ok_fii_stats, ok_participant_oi = run_single(d)

    # Expose outcome for the separate telegram_alert.py workflow step.
    # This script does nothing with Telegram itself -- see fii-derivatives.yml
    # for how these two flags get turned into a success/partial/failure status.
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"fii_stats_ok={'true' if ok_fii_stats else 'false'}\n")
            f.write(f"participant_oi_ok={'true' if ok_participant_oi else 'false'}\n")
            f.write(f"fii_date={d}\n")


if __name__ == "__main__":
    main()
