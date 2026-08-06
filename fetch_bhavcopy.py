"""
Runs on GitHub Actions (see .github/workflows/bhavcopy.yml), independent of
Render entirely. Fetches that trading day's NSE F&O bhavcopy, stores the
full file in the shared data repo under options-bhavcopy/, strips it down
to a Nifty-only file (options-bhavcopy/nifty-only/) covering just the two
nearest expiries for the live Render app to fetch cheaply, appends the
day's real 15:41 closing-summary row to that day's nifty-open-interest/oi_{date}.csv,
and cleans up old files past their retention window (90 days for bhavcopy
files, 3 days for the live app's own intraday chunk files under
nifty-open-interest/intraday/ -- see INTRADAY_RETENTION_DAYS for why that
one's so much shorter).

Everything this script needs comes from the shared repo itself via the
GitHub API -- it never talks to the live Flask app or Render at all:
  - Anchor + tracked strikes for today: read from today's own
    nifty-open-interest/oi_{date}.csv, which the main app has already been
    writing to all day (the 09:14 row).
  - Expiry for today: derived from the bhavcopy's own expiry listing (the
    nearest listed expiry >= today), same rule locked in the main spec.

The full bhavcopy is fetched at most once per run (cached in the local
csv_text variable inside main()) and reused by every step that needs it --
the Nifty-only strip and the 15:41 row calc both read from that same
content instead of each re-fetching the file independently.

Idempotent by design: every one of the day's 3 scheduled runs (10:00 PM,
10:30 PM, 11:30 PM IST) calls this same script. Each step checks whether
its own output already exists before doing any work, so a run that finds
everything already done just exits quickly -- no duplicate pushes, no
double-counted retries.
"""
import base64
import csv
import io
import os
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone

import requests

# ---- Config -----------------------------------------------------------
DATA_REPO = os.environ.get("DATA_REPO", "amol-bendre/stock-market-data")
TOKEN = os.environ["DATA_REPO_TOKEN"]  # GitHub Actions secret, write access to DATA_REPO
IST = timezone(timedelta(hours=5, minutes=30))
RETENTION_DAYS = 90
INTRADAY_RETENTION_DAYS = 3  # much shorter than bhavcopy retention -- these files exist purely
                             # for same-day crash recovery (see cloud_app.py's
                             # push_chunk_to_github()/_pull_date_into_db()), fully superseded by
                             # that day's consolidated oi_{date}.csv every single day, never read
                             # back for any historical purpose the way bhavcopy files sometimes are

NSE_URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip"
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


def today_ist() -> date:
    override = os.environ.get("TARGET_DATE", "").strip()
    if override:
        return date.fromisoformat(override)
    return datetime.now(IST).date()


# ---- GitHub Contents API helpers --------------------------------------
def gh_fetch_raw(path):
    """
    Returns the file's raw text content, or None if not found.

    GitHub's Contents API only returns content inline (base64, in the JSON
    'content' field) for files 1 MB or smaller -- for anything larger, that
    field comes back silently EMPTY while the request still reports success
    (200). Every bhavcopy/{date}.csv file is ~7 MB, so the old approach
    here (base64.b64decode(j["content"])) would always decode to an empty
    string rather than raise an error -- and since an empty string is not
    None, the calling code's "not found" check would never catch it,
    letting it proceed straight into a crash further downstream instead of
    failing cleanly.

    Requesting the 'raw' media type bypasses the inline-JSON envelope
    entirely and returns the actual file bytes directly -- correct for any
    file up to 100 MB, regardless of size.
    """
    headers = {**GH_HEADERS, "Accept": "application/vnd.github.v3.raw"}
    r = requests.get(f"{GH_API}/repos/{DATA_REPO}/contents/{path}", headers=headers, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.text

def gh_get_sha(path):
    """Returns the file's current sha (needed to update it via PUT), or
    None if not found. Independent of file size, since the sha is always
    present in the JSON envelope regardless of content-field length."""
    r = requests.get(f"{GH_API}/repos/{DATA_REPO}/contents/{path}", headers=GH_HEADERS, timeout=15)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json().get("sha")


def gh_put_file(path, content_str, message, sha=None):
    payload = {"message": message, "content": base64.b64encode(content_str.encode()).decode()}
    if sha:
        payload["sha"] = sha
    r = requests.put(f"{GH_API}/repos/{DATA_REPO}/contents/{path}", headers=GH_HEADERS, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def gh_delete_file(path, sha, message):
    r = requests.delete(
        f"{GH_API}/repos/{DATA_REPO}/contents/{path}", headers=GH_HEADERS,
        json={"message": message, "sha": sha}, timeout=15,
    )
    r.raise_for_status()


def gh_list_dir(path):
    r = requests.get(f"{GH_API}/repos/{DATA_REPO}/contents/{path}", headers=GH_HEADERS, timeout=15)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return r.json()


# ---- NSE fetch ----------------------------------------------------------
def fetch_nse_bhavcopy(d: date):
    """Returns CSV text, or None if this was a non-trading day (clean 404)."""
    url = NSE_URL.format(ymd=d.strftime("%Y%m%d"))
    r = requests.get(url, headers=NSE_HEADERS, timeout=20)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    csv_text = zf.read(zf.namelist()[0]).decode("utf-8")

    expected = d.strftime("%Y-%m-%d")
    first_line = csv_text.splitlines()[1]
    if not first_line.startswith(expected):
        raise ValueError(f"TradDt mismatch: expected rows starting '{expected}', got '{first_line[:30]}'")
    return csv_text


def parse_nifty_ido(csv_text):
    """{(strike:int, 'CE'/'PE', expiry_str): oi:int}, plus the set of all expiries seen."""
    data, expiries = {}, set()
    for row in csv.DictReader(csv_text.splitlines()):
        if row.get("TckrSymb") != "NIFTY" or row.get("FinInstrmTp") != "IDO":
            continue
        strike = int(float(row["StrkPric"]))
        data[(strike, row["OptnTp"], row["XpryDt"])] = int(float(row["OpnIntrst"]))
        expiries.add(row["XpryDt"])
    return data, expiries


def nearest_expiry(expiries, d: date):
    ds = sorted(date.fromisoformat(e) for e in expiries)
    return min(e for e in ds if e >= d).isoformat()


# ---- Step 1: cache full bhavcopy to bhavcopy/{date}.csv ----------------
def ensure_bhavcopy_cached(d: date):
    """
    Returns the full bhavcopy CSV text -- whether freshly fetched just
    now, or already cached from an earlier run today -- or None if NSE
    hasn't published it yet. Returning the actual content (not just a
    bool, as this used to) means every caller that needs it (the new
    Nifty-only strip step, the 15:41 row calc) gets it from this single
    fetch instead of each independently re-fetching the same file again.
    """
    path = f"options-bhavcopy/fno_bhavcopy_{d}.csv"
    existing = gh_fetch_raw(path)
    if existing is not None:
        print(f"[bhavcopy] {path} already cached, skipping fetch")
        return existing

    print(f"[bhavcopy] fetching NSE bhavcopy for {d} ...")
    csv_text = fetch_nse_bhavcopy(d)
    if csv_text is None:
        print(f"[bhavcopy] NSE returned 404 for {d} -- not a trading day (or not yet published)")
        return None

    gh_put_file(path, csv_text, f"Bhavcopy {d}")
    print(f"[bhavcopy] pushed {path} ({len(csv_text):,} bytes)")
    return csv_text


def cleanup_old_bhavcopies(today: date):
    cutoff = today - timedelta(days=RETENTION_DAYS)
    PREFIX, SUFFIX = "fno_bhavcopy_", ".csv"
    for entry in gh_list_dir("options-bhavcopy"):
        name = entry["name"]
        if not (name.startswith(PREFIX) and name.endswith(SUFFIX)):
            continue
        try:
            d = date.fromisoformat(name[len(PREFIX):-len(SUFFIX)])
        except ValueError:
            continue
        if d < cutoff:
            gh_delete_file(f"options-bhavcopy/{name}", entry["sha"], f"Retention cleanup: remove {name}")
            print(f"[cleanup] deleted options-bhavcopy/{name} (older than {RETENTION_DAYS} days)")


# ---- Step 1b: strip to Nifty-only, nearest 2 expiries -------------------
def ensure_nifty_only_cached(d: date, csv_text: str):
    """
    Strips the full whole-market bhavcopy down to just Nifty index
    options (TckrSymb=='NIFTY', FinInstrmTp=='IDO'), keeping only the
    two nearest expiries and the 4 columns anything downstream actually
    needs (strike, side, expiry, OI). The full file is the entire
    exchange's F&O universe -- every stock, every index, every strike,
    every expiry, dozens of columns each; this is a tiny fraction of
    that, existing purely so the live Render app can fetch a KB-scale
    file instead of a multi-MB one every time it needs previous-day
    anchor OI.

    Two expiries, not just the nearest one, so a rollover day -- where
    the live app's own logic decides to switch from the expiring
    contract to the next one -- is never left anchoring against a file
    that only has the expiry it just rolled away from. This script
    doesn't need to know anything about that rollover logic itself: it
    just keeps whichever two expiries are chronologically soonest in
    today's bhavcopy, whatever those happen to be, and lets the app
    decide which one it wants.

    Idempotent like every other step here: skips entirely if today's
    stripped file already exists.
    """
    path = f"options-bhavcopy/nifty-only/fno_bhavcopy_nifty_{d}.csv"
    if gh_fetch_raw(path) is not None:
        print(f"[nifty-only] {path} already cached, skipping")
        return

    data, expiries = parse_nifty_ido(csv_text)
    if not expiries:
        print(f"[nifty-only] no NIFTY/IDO rows found in today's bhavcopy -- skipping")
        return

    # XpryDt strings are already ISO format (YYYY-MM-DD), confirmed by
    # nearest_expiry() elsewhere in this file calling date.fromisoformat()
    # on them directly -- so plain string sort already sorts chronologically,
    # no need to parse into date objects first.
    nearest_two = sorted(expiries)[:2]

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["strike", "option_type", "expiry", "oi"])
    for (strike, side, expiry), oi in sorted(data.items()):
        if expiry in nearest_two:
            w.writerow([strike, side, expiry, oi])

    content = out.getvalue()
    gh_put_file(path, content, f"Nifty-only bhavcopy {d} (expiries {', '.join(nearest_two)})")
    print(f"[nifty-only] pushed {path} ({len(content):,} bytes, expiries {nearest_two})")


def cleanup_old_nifty_only(today: date):
    cutoff = today - timedelta(days=RETENTION_DAYS)
    PREFIX, SUFFIX = "fno_bhavcopy_nifty_", ".csv"
    for entry in gh_list_dir("options-bhavcopy/nifty-only"):
        name = entry["name"]
        if not (name.startswith(PREFIX) and name.endswith(SUFFIX)):
            continue
        try:
            d = date.fromisoformat(name[len(PREFIX):-len(SUFFIX)])
        except ValueError:
            continue
        if d < cutoff:
            gh_delete_file(f"options-bhavcopy/nifty-only/{name}", entry["sha"], f"Retention cleanup: remove {name}")
            print(f"[cleanup] deleted options-bhavcopy/nifty-only/{name} (older than {RETENTION_DAYS} days)")


def cleanup_old_intraday_chunks(today: date):
    """
    Deletes intraday chunk files (nifty-open-interest/intraday/, written
    all day by cloud_app.py's push_chunk_to_github()) older than
    INTRADAY_RETENTION_DAYS. Much shorter window than the other two
    cleanups here -- see INTRADAY_RETENTION_DAYS's own comment for why.

    Filenames are oi_{date}_{HHMM}.csv -- the trailing _HHMM (added
    after the date, unlike the other two cleanup targets here) has to
    be split off before the remaining text is a bare date string.
    Splitting on the *last* underscore does this correctly: the date
    portion itself never contains one, so whatever's after the final
    underscore is always the HHMM part, whatever's before is always the
    complete date.
    """
    cutoff = today - timedelta(days=INTRADAY_RETENTION_DAYS)
    PREFIX, SUFFIX = "oi_", ".csv"
    for entry in gh_list_dir("nifty-open-interest/intraday"):
        name = entry["name"]
        if not (name.startswith(PREFIX) and name.endswith(SUFFIX)):
            continue
        middle = name[len(PREFIX):-len(SUFFIX)]  # e.g. "2026-08-04_1721"
        date_part = middle.rsplit("_", 1)[0]      # -> "2026-08-04"
        try:
            d = date.fromisoformat(date_part)
        except ValueError:
            continue
        if d < cutoff:
            gh_delete_file(f"nifty-open-interest/intraday/{name}", entry["sha"], f"Retention cleanup: remove {name}")
            print(f"[cleanup] deleted nifty-open-interest/intraday/{name} (older than {INTRADAY_RETENTION_DAYS} days)")


# ---- Step 2: append today's real 15:41 row into data/{date}.csv --------
def ensure_1541_row(d: date, csv_text: str):
    data_path = f"nifty-open-interest/oi_{d}.csv"
    content = gh_fetch_raw(data_path)
    if content is None:
        print(f"[15:41] {data_path} not found yet (live app may not have pushed today's data) -- skipping")
        return
    sha = gh_get_sha(data_path)

    rows = list(csv.DictReader(content.splitlines()))
    if any(r["time"] == "15:41" for r in rows):
        print(f"[15:41] {data_path} already has a 15:41 row, skipping")
        return

    anchor_rows = [r for r in rows if r["time"] == "09:14"]
    if not anchor_rows:
        print(f"[15:41] no 09:14 anchor rows found in {data_path} -- skipping")
        return

    own_bhav, own_expiries = parse_nifty_ido(csv_text)
    if not own_expiries:
        # Defensive: covers any other reason the bhavcopy came back
        # readable-but-empty, not just the size bug above -- nearest_expiry
        # would otherwise raise on an empty sequence.
        print(f"[15:41] today's bhavcopy had no usable NIFTY rows -- skipping")
        return
    expiry_today = nearest_expiry(own_expiries, d)

    new_rows = []
    for r in anchor_rows:
        strike = int(r["strike"])
        aco, apo = int(r["call_oi"]), int(r["put_oi"])
        ce = own_bhav.get((strike, "CE", expiry_today))
        pe = own_bhav.get((strike, "PE", expiry_today))
        if ce is None or pe is None:
            continue
        new_rows.append({
            "date": str(d), "time": "15:41", "strike": strike, "expiry": expiry_today,
            "call_oi": ce, "put_oi": pe,
            "call_chg": ce - aco, "put_chg": pe - apo, "ticks": 0,
        })

    if not new_rows:
        print(f"[15:41] no matching strikes found in bhavcopy for {d} -- skipping")
        return

    fieldnames = list(rows[0].keys()) if rows else \
        ["date", "time", "strike", "expiry", "call_oi", "put_oi", "call_chg", "put_chg", "ticks"]
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=fieldnames)
    w.writeheader()
    w.writerows(rows + new_rows)

    gh_put_file(data_path, out.getvalue(), f"Add 15:41 closing row for {d}", sha=sha)
    print(f"[15:41] appended {len(new_rows)} rows to {data_path}")


def main():
    d = today_ist()
    print(f"=== Bhavcopy job for {d} (IST) ===")

    csv_text = ensure_bhavcopy_cached(d)
    cleanup_old_bhavcopies(d)
    cleanup_old_nifty_only(d)
    cleanup_old_intraday_chunks(d)

    if csv_text is not None:
        ensure_nifty_only_cached(d, csv_text)
        ensure_1541_row(d, csv_text)
    else:
        print("Skipping Nifty-only + 15:41 steps -- no bhavcopy available for today")

    # Expose outcome for the separate telegram_alert.py workflow step.
    # This script does nothing with Telegram itself -- it only reports
    # what happened so a fully independent script can decide whether to alert.
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"available={'true' if csv_text is not None else 'false'}\n")
            f.write(f"bhav_date={d}\n")


if __name__ == "__main__":
    main()
