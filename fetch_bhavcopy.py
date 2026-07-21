"""
Runs on GitHub Actions (see .github/workflows/bhavcopy.yml), independent of
Render entirely. Fetches that trading day's NSE F&O bhavcopy, stores the
full file in the private data repo under bhavcopy/, and appends the day's
real 15:31 closing-summary row to that day's data/{date}.csv.

Everything this script needs comes from the private repo itself via the
GitHub API -- it never talks to the live Flask app or Render at all:
  - Anchor + tracked strikes for today: read from today's own data/{date}.csv,
    which the main app has already been writing to all day (the 09:14 row).
  - Expiry for today: derived from the bhavcopy's own expiry listing (the
    nearest listed expiry >= today), same rule locked in the main spec.

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
DATA_REPO = os.environ.get("DATA_REPO", "amol-bendre/nifty-oi-data")
TOKEN = os.environ["DATA_REPO_TOKEN"]  # GitHub Actions secret, write access to DATA_REPO
IST = timezone(timedelta(hours=5, minutes=30))
RETENTION_DAYS = 90

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
def gh_get_file(path):
    """Returns (content_str, sha) or (None, None) if not found."""
    r = requests.get(f"{GH_API}/repos/{DATA_REPO}/contents/{path}", headers=GH_HEADERS, timeout=15)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    j = r.json()
    content = base64.b64decode(j["content"]).decode("utf-8")
    return content, j["sha"]


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
    path = f"bhavcopy/{d}.csv"
    existing, _ = gh_get_file(path)
    if existing is not None:
        print(f"[bhavcopy] {path} already cached, skipping fetch")
        return True

    print(f"[bhavcopy] fetching NSE bhavcopy for {d} ...")
    csv_text = fetch_nse_bhavcopy(d)
    if csv_text is None:
        print(f"[bhavcopy] NSE returned 404 for {d} -- not a trading day (or not yet published)")
        return False

    gh_put_file(path, csv_text, f"Bhavcopy {d}")
    print(f"[bhavcopy] pushed {path} ({len(csv_text):,} bytes)")
    return True


def cleanup_old_bhavcopies(today: date):
    cutoff = today - timedelta(days=RETENTION_DAYS)
    for entry in gh_list_dir("bhavcopy"):
        name = entry["name"]
        if not name.endswith(".csv"):
            continue
        try:
            d = date.fromisoformat(name[:-4])
        except ValueError:
            continue
        if d < cutoff:
            gh_delete_file(f"bhavcopy/{name}", entry["sha"], f"Retention cleanup: remove {name}")
            print(f"[cleanup] deleted bhavcopy/{name} (older than {RETENTION_DAYS} days)")


# ---- Step 2: append today's real 15:31 row into data/{date}.csv --------
def ensure_1531_row(d: date):
    data_path = f"data/{d}.csv"
    content, sha = gh_get_file(data_path)
    if content is None:
        print(f"[15:31] {data_path} not found yet (live app may not have pushed today's data) -- skipping")
        return

    rows = list(csv.DictReader(content.splitlines()))
    if any(r["time"] == "15:31" for r in rows):
        print(f"[15:31] {data_path} already has a 15:31 row, skipping")
        return

    anchor_rows = [r for r in rows if r["time"] == "09:14"]
    if not anchor_rows:
        print(f"[15:31] no 09:14 anchor rows found in {data_path} -- skipping")
        return

    bhav_content, _ = gh_get_file(f"bhavcopy/{d}.csv")
    if bhav_content is None:
        print(f"[15:31] bhavcopy/{d}.csv not available -- skipping")
        return
    own_bhav, own_expiries = parse_nifty_ido(bhav_content)
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
            "date": str(d), "time": "15:31", "strike": strike, "expiry": expiry_today,
            "call_oi": ce, "put_oi": pe,
            "call_chg": ce - aco, "put_chg": pe - apo, "ticks": 0,
        })

    if not new_rows:
        print(f"[15:31] no matching strikes found in bhavcopy for {d} -- skipping")
        return

    fieldnames = list(rows[0].keys()) if rows else \
        ["date", "time", "strike", "expiry", "call_oi", "put_oi", "call_chg", "put_chg", "ticks"]
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=fieldnames)
    w.writeheader()
    w.writerows(rows + new_rows)

    gh_put_file(data_path, out.getvalue(), f"Add 15:31 closing row for {d}", sha=sha)
    print(f"[15:31] appended {len(new_rows)} rows to {data_path}")


def main():
    d = today_ist()
    print(f"=== Bhavcopy job for {d} (IST) ===")

    ok = ensure_bhavcopy_cached(d)
    cleanup_old_bhavcopies(d)

    if ok:
        ensure_1531_row(d)
    else:
        print("Skipping 15:31 step -- no bhavcopy available for today")


if __name__ == "__main__":
    main()
