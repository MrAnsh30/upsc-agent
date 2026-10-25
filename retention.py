"""Retention lifecycle for topic threads.

Rules
-----
* A thread has Status ONGOING or ENDED.
* ONGOING threads are never purged (covers the "still running after 5 years" rule).
* When a thread ENDS:  expiry = max(start + 5 years, end + 2 years)
    - ended inside the first 5 years  -> purged at start + 5y   (default rule)
    - ended after year 3              -> end + 2y beats start + 5y (post-end extension)
    - ongoing past year 5, ends later -> end + 2y   (i.e. total lifecycle 7+ years)
* One-off news is stored as a single-item thread that is closed immediately, so
  expiry = item date + 5 years.
* Safety valve: threads with no update for a long time are auto-closed (STANDARD 90d,
  LONG_RUNNING 365d) so nothing stays "ongoing" forever by accident.
"""
from __future__ import annotations

from datetime import date

BASE_YEARS = 5
POST_END_EXTENSION_YEARS = 2
STALE_DAYS = {"STANDARD": 90, "LONG_RUNNING": 365}


def add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:  # 29 Feb -> 28 Feb
        return d.replace(year=d.year + n, day=28)


def parse(s: str | None) -> date | None:
    s = (s or "").strip()
    try:
        return date.fromisoformat(s[:10]) if s else None
    except ValueError:
        return None


def expiry_date(start: date, status: str, end: date | None) -> date | None:
    """None means 'do not purge' (thread is still ongoing)."""
    if status != "ENDED" or end is None:
        return None
    return max(add_years(start, BASE_YEARS), add_years(end, POST_END_EXTENSION_YEARS))


def expiry_label(start: date, status: str, end: date | None) -> str:
    d = expiry_date(start, status, end)
    return "RETAINED (ongoing)" if d is None else d.isoformat()


def should_purge(start: date, status: str, end: date | None, today: date) -> bool:
    d = expiry_date(start, status, end)
    return d is not None and today >= d


def should_autoclose(thread_type: str, status: str, last_update: date, today: date) -> bool:
    if status != "ONGOING":
        return False
    return (today - last_update).days > STALE_DAYS.get(thread_type, STALE_DAYS["STANDARD"])


if __name__ == "__main__":  # quick self-test
    d = date.fromisoformat
    # Russia-Ukraine: started 2022-02-24, still ongoing in 2027 -> never purged
    assert not should_purge(d("2022-02-24"), "ONGOING", None, d("2027-03-01"))
    # ends 2027-06-01 (after 5y mark) -> kept until 2029-06-01
    assert expiry_date(d("2022-02-24"), "ENDED", d("2027-06-01")) == d("2029-06-01")
    assert not should_purge(d("2022-02-24"), "ENDED", d("2027-06-01"), d("2029-05-31"))
    assert should_purge(d("2022-02-24"), "ENDED", d("2027-06-01"), d("2029-06-01"))
    # short event ended after 1 month -> default 5y rule
    assert expiry_date(d("2024-01-10"), "ENDED", d("2024-02-10")) == d("2029-01-10")
    # one-off item on 29 Feb 2024
    assert expiry_date(d("2024-02-29"), "ENDED", d("2024-02-29")) == d("2029-02-28")
    assert should_autoclose("STANDARD", "ONGOING", d("2026-01-01"), d("2026-06-01"))
    assert not should_autoclose("LONG_RUNNING", "ONGOING", d("2026-01-01"), d("2026-06-01"))
    print("retention self-test OK")
