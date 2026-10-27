#!/usr/bin/env python3
"""Script 4 — Delete each short video N days (default 10) after it was published.

Deletes, for every expired record in state/published.json:
  * the YouTube Short          (YouTube Data API v3  videos.delete)
  * the Instagram Reel         (Graph API  DELETE /{media-id}; needs the instagram_manage_contents permission)
  * the local video file       (plus any leftover local .mp4 / temp audio older than N days)

It NEVER deletes or clears the Google Sheet. The only thing it touches there is the video-link cell of rows
whose video was just removed, so students don't click a dead link (the row itself stays forever).

Safe by design: a record is kept (and retried every day) until every platform confirms the deletion.
Set CLEANUP_DRY_RUN=1 to only print what would be deleted.
"""
from __future__ import annotations

import datetime as dt
import shutil
import sys
from pathlib import Path

import requests

import state_store
from common import NEWS_SHEET, OUT, ROOT, TOPIC_SHEET, env, open_spreadsheet

DAYS = int(env("VIDEO_RETENTION_DAYS", "10"))
DRY = env("CLEANUP_DRY_RUN", "0") == "1"
GRAPH = f"https://graph.facebook.com/{env('GRAPH_VERSION', 'v23.0')}"


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def is_due(rec: dict, now: dt.datetime) -> bool:
    try:
        published = dt.datetime.fromisoformat(rec["published_at"])
    except (KeyError, ValueError):
        return False
    return now - published >= dt.timedelta(days=DAYS)


# ----------------------------------------------------------------------------- platforms
def yt_delete(video_id: str) -> None:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    creds = Credentials(None, refresh_token=env("YT_REFRESH_TOKEN", required=True),
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=env("YT_CLIENT_ID", required=True),
                        client_secret=env("YT_CLIENT_SECRET", required=True),
                        scopes=["https://www.googleapis.com/auth/youtube"])
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    try:
        yt.videos().delete(id=video_id).execute()
    except HttpError as e:
        if e.resp.status == 404:  # already removed (e.g. by hand) -> that is fine
            return
        raise


def ig_delete(media_id: str) -> None:
    token = env("IG_ACCESS_TOKEN", required=True)
    r = requests.delete(f"{GRAPH}/{media_id}", params={"access_token": token}, timeout=60)
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:200]}
    if r.status_code == 200 and data.get("success", True):
        return
    raise RuntimeError(f"Instagram delete failed (HTTP {r.status_code}): {data}")


# ----------------------------------------------------------------------------- local files
def delete_local(rel_path: str) -> None:
    p = (ROOT / rel_path) if rel_path else None
    if p and p.exists():
        p.unlink()
        print(f"[local] deleted {rel_path}")


def sweep_local(now: dt.datetime) -> None:
    cutoff = now.timestamp() - DAYS * 86400
    for p in list((OUT / "videos").glob("*.mp4")) + list((OUT / "tmp").rglob("*.mp3")):
        if p.stat().st_mtime < cutoff:
            print(f"[local] {'would delete' if DRY else 'deleted'} old file {p.relative_to(ROOT)}")
            if not DRY:
                p.unlink()
    tmp = OUT / "tmp"
    if tmp.exists() and not DRY:
        for d in tmp.iterdir():
            if d.is_dir() and not any(d.iterdir()):
                shutil.rmtree(d, ignore_errors=True)


# ----------------------------------------------------------------------------- sheet (links only, never rows)
def mark_links_removed(removed_urls: set[str]) -> None:
    if not removed_urls:
        return
    from gspread.utils import rowcol_to_a1

    sh = open_spreadsheet()
    ws_n, ws_t = sh.worksheet(NEWS_SHEET), sh.worksheet(TOPIC_SHEET)
    msg = f"Video removed after {DAYS} days"
    for ws, col_name in ((ws_n, "Related_Video_URL"), (ws_t, "Latest_Video_URL")):
        col = ws.row_values(1).index(col_name) + 1
        vals = ws.col_values(col)
        updates = [{"range": rowcol_to_a1(i + 1, col), "values": [[msg]]}
                   for i, v in enumerate(vals) if i > 0 and v in removed_urls]
        if updates:
            ws.batch_update(updates, value_input_option="RAW")
            print(f"[sheet] {ws.title}: {len(updates)} link cell(s) marked as removed (rows kept)")


# ----------------------------------------------------------------------------- main
def main() -> int:
    now = utcnow()
    records = state_store.load()
    keep: list[dict] = []
    removed_urls: set[str] = set()
    failures = 0
    print(f"[info] {len(records)} published record(s); deleting those older than {DAYS} days"
          + ("  (DRY RUN)" if DRY else ""))

    for rec in records:
        if not is_due(rec, now):
            keep.append(rec)
            continue
        label = rec.get("title", "?")[:60]
        print(f"[due] {label} (published {rec['published_at']})")
        if DRY:
            print(f"      would delete YouTube={rec.get('youtube_id') or '-'}  Instagram={rec.get('instagram_id') or '-'}"
                  f"  local={rec.get('file') or '-'}")
            keep.append(rec)
            continue

        if rec.get("youtube_id"):
            try:
                yt_delete(rec["youtube_id"])
                removed_urls.add(rec["youtube_url"])
                rec["youtube_id"] = ""
                print("      YouTube: deleted")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"      [error] YouTube delete failed, will retry tomorrow: {e}")
        if rec.get("instagram_id"):
            try:
                ig_delete(rec["instagram_id"])
                removed_urls.add(rec["instagram_url"])
                rec["instagram_id"] = ""
                print("      Instagram: deleted")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"      [error] Instagram delete failed, will retry tomorrow: {e}")

        delete_local(rec.get("file", ""))
        if rec.get("youtube_id") or rec.get("instagram_id"):
            keep.append(rec)        # something is still online -> keep the record and retry
        else:
            print("      record complete and removed from tracking")

    if not DRY:
        state_store.save(keep)
        sweep_local(now)
        try:
            mark_links_removed(removed_urls)
        except Exception as e:  # noqa: BLE001 - a Sheet hiccup must never undo the deletions
            print(f"[warn] could not update link cells in the Sheet: {e}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
