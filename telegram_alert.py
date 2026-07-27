"""
Generic, job-agnostic Telegram status reporter -- shared by every fetch job
in this repo. This script knows nothing about bhavcopies, FII reports, or
any other job-specific concept: it only knows how to turn one or more
(label, status, detail, date) blocks into message lines and send them as a
single Telegram message. All job-specific logic (what counts as success vs.
partial vs. failure, and which detail to show) lives in the calling
workflow's own YAML, where it belongs.

Sends a message only on the last scheduled attempt for the day (controlled
entirely by the calling workflow's IS_LAST_ATTEMPT flag) -- earlier retries
never trigger anything from this script.

Multi-job mode (current usage, from daily-fetch.yml's shared `alert` job):
  Reads JOB1_LABEL/JOB1_STATUS/JOB1_DETAIL/JOB1_DATE, JOB2_..., JOB3_..., and
  so on -- as many numbered blocks as are set, stopping at the first missing
  LABEL. Each block becomes one line; all lines are sent as a single
  message. This is how "Bhavcopy" and "FII Reports" (or any future third
  fetcher, just by adding a JOB3_* block -- no script changes needed) end
  up combined into one Telegram message instead of two separate ones.

Legacy single-job mode (fallback, for any workflow not yet using numbered
blocks): reads JOB_LABEL/STATUS/DETAIL/REPORT_DATE directly, same as the
original version of this script.

Status values per block:
  "success" -> "fetched for {date}"
  "partial" -> "partially fetched for {date} -- {detail}"
  "failure" -> "NOT available for {date} -- all attempts failed"

Wrapped in try/except throughout: a Telegram-side failure (bad token,
network issue, API outage) is logged and swallowed here, and never fails
the calling workflow's job.
"""
import os
import sys

import requests

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
IS_LAST_ATTEMPT = os.environ.get("IS_LAST_ATTEMPT", "false").strip().lower() == "true"


def line_for_job(label: str, status: str, detail: str, date: str):
    report_date = date.strip() or "(date unknown)"
    status = status.strip().lower()
    detail = detail.strip()

    if status == "success":
        return f"✅ {label} fetched for {report_date}"
    if status == "partial":
        suffix = f" — {detail}" if detail else ""
        return f"⚠️ {label} partially fetched for {report_date}{suffix}"
    if status == "failure":
        return f"❌ {label} NOT available for {report_date} — all attempts failed"

    print(f"[telegram_alert] unrecognized STATUS={status!r} for job {label!r} -- skipping this line")
    return None


def collect_job_blocks():
    """Numbered blocks (JOB1_*, JOB2_*, ...) take priority. Falls back to
    the legacy single-job env vars only if no numbered blocks are present
    at all."""
    blocks = []
    n = 1
    while True:
        label = os.environ.get(f"JOB{n}_LABEL", "").strip()
        if not label:
            break
        blocks.append({
            "label": label,
            "status": os.environ.get(f"JOB{n}_STATUS", ""),
            "detail": os.environ.get(f"JOB{n}_DETAIL", ""),
            "date": os.environ.get(f"JOB{n}_DATE", ""),
        })
        n += 1

    if blocks:
        return blocks

    legacy_label = os.environ.get("JOB_LABEL", "").strip()
    if legacy_label:
        return [{
            "label": legacy_label,
            "status": os.environ.get("STATUS", ""),
            "detail": os.environ.get("DETAIL", ""),
            "date": os.environ.get("REPORT_DATE", ""),
        }]

    return []


def build_message():
    blocks = collect_job_blocks()
    if not blocks:
        print("[telegram_alert] no JOB*_LABEL / JOB_LABEL env vars set -- nothing to report")
        return None

    lines = [line_for_job(**b) for b in blocks]
    lines = [l for l in lines if l is not None]
    if not lines:
        return None
    return "\n".join(lines)


def send_telegram_message(text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=15)
    r.raise_for_status()


def main():
    if not IS_LAST_ATTEMPT:
        print("[telegram_alert] not the last scheduled attempt -- staying silent")
        return

    if not BOT_TOKEN or not CHAT_ID:
        print("[telegram_alert] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set -- skipping alert")
        return

    message = build_message()
    if message is None:
        return

    print(f"[telegram_alert] last attempt -- sending:\n{message}")
    try:
        send_telegram_message(message)
        print("[telegram_alert] message sent successfully")
    except Exception as e:
        # Never let a Telegram-side problem fail the calling job.
        print(f"[telegram_alert] failed to send Telegram message: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
