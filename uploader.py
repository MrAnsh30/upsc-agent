#!/usr/bin/env python3
"""Script 3 — Publish videos from output/manifest.json to YouTube Shorts and Instagram Reels,
then write the resulting URLs back into the Master Sheet (Related_Video_URL / Latest_Video_URL).

YouTube : YouTube Data API v3 resumable upload (OAuth refresh token).
Instagram: Instagram Graph API Reels publishing. Meta must be able to download the video from a
           public URL, so we stage it on Cloudinary's free tier and delete it afterwards.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
import time

import requests

import state_store
from common import NEWS_SHEET, OUT, ROOT, TOPIC_SHEET, env, open_spreadsheet

GRAPH = f"https://graph.facebook.com/{env('GRAPH_VERSION', 'v23.0')}"   # bump if Meta retires the version
DO_YT, DO_IG = env("UPLOAD_YT", "1") == "1", env("UPLOAD_IG", "1") == "1"


def build_description(meta: dict, sheet_url: str, for_ig: bool = False) -> str:
    lines = [f"• {i['headline']} ({i['gs_paper']})" for i in meta["items"]]
    head = "UPSC Current Affairs – quick revision\n\n" + "\n".join(lines)
    days = env("VIDEO_RETENTION_DAYS", "10")
    link = (f"\n\n📚 Full thread-wise revision sheet (GS-mapped, updated daily): {sheet_url}"
            f"\n📚 पूरी रिवीज़न शीट (GS मैपिंग के साथ, रोज़ अपडेट होती है): {sheet_url}"
            f"\nℹ️ This short is removed after {days} days — the Sheet always stays online and keeps growing."
            + ("\n(Link in bio too — Instagram captions aren't clickable.)" if for_ig else ""))
    credits = ("\n\nImage credits: " + "; ".join(meta["credits"])) if meta["credits"] else ""
    tags = "\n\n" + " ".join(meta["hashtags"] + ["#CurrentAffairs", "#UPSC2027", "#IAS"])
    if not for_ig:
        tags += " #Shorts"
    text = head + link + credits + tags
    return text[:2150] if for_ig else text[:4900]


# ----------------------------------------------------------------------------- YouTube
def yt_upload(meta: dict, sheet_url: str) -> str:
    """Uploads and returns the YouTube video ID."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(None, refresh_token=env("YT_REFRESH_TOKEN", required=True),
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=env("YT_CLIENT_ID", required=True),
                        client_secret=env("YT_CLIENT_SECRET", required=True),
                        scopes=["https://www.googleapis.com/auth/youtube"])  # full scope: needed to delete later
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    body = {
        "snippet": {"title": f"{meta['title'][:88]} #Shorts", "description": build_description(meta, sheet_url),
                    "tags": [h.lstrip("#") for h in meta["hashtags"]][:15] + ["UPSC", "current affairs"],
                    "categoryId": "27", "defaultLanguage": "en"},
        "status": {"privacyStatus": env("YT_PRIVACY", "public"), "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(meta["file"], mimetype="video/mp4", chunksize=-1, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp, tries = None, 0
    while resp is None:
        try:
            _, resp = req.next_chunk()
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504) and tries < 5:
                tries += 1
                time.sleep(2 ** tries)
                continue
            raise
    return resp["id"]


# ----------------------------------------------------------------------------- Cloudinary staging
def _cld_sign(params: dict, secret: str) -> str:
    to_sign = "&".join(f"{k}={v}" for k, v in sorted(params.items())) + secret
    return hashlib.sha1(to_sign.encode()).hexdigest()


def cloudinary_upload(path: str) -> tuple[str, str]:
    cloud, key, secret = (env("CLOUDINARY_CLOUD_NAME", required=True), env("CLOUDINARY_API_KEY", required=True),
                          env("CLOUDINARY_API_SECRET", required=True))
    pid, ts = f"upsc_{int(time.time())}", int(time.time())
    sig = _cld_sign({"public_id": pid, "timestamp": ts}, secret)
    with open(path, "rb") as f:
        r = requests.post(f"https://api.cloudinary.com/v1_1/{cloud}/video/upload",
                          data={"api_key": key, "timestamp": ts, "public_id": pid, "signature": sig},
                          files={"file": f}, timeout=300)
    r.raise_for_status()
    return r.json()["secure_url"], pid


def cloudinary_delete(pid: str) -> None:
    cloud, key, secret = (env("CLOUDINARY_CLOUD_NAME"), env("CLOUDINARY_API_KEY"), env("CLOUDINARY_API_SECRET"))
    ts = int(time.time())
    sig = _cld_sign({"public_id": pid, "timestamp": ts}, secret)
    try:
        requests.post(f"https://api.cloudinary.com/v1_1/{cloud}/video/destroy", timeout=60,
                      data={"api_key": key, "timestamp": ts, "public_id": pid, "signature": sig})
    except Exception as e:  # noqa: BLE001
        print(f"[warn] cloudinary cleanup failed: {e}")


# ----------------------------------------------------------------------------- Instagram Reels
def _graph(method: str, path: str, **kw) -> dict:
    r = requests.request(method, f"{GRAPH}/{path}", timeout=120, **kw)
    data = r.json()
    if r.status_code >= 400 or "error" in data:
        raise RuntimeError(f"Graph API error: {data}")
    return data


def ig_publish(meta: dict, sheet_url: str) -> tuple[str, str]:
    """Publishes the Reel and returns (media_id, permalink)."""
    ig_id, token = env("IG_USER_ID", required=True), env("IG_ACCESS_TOKEN", required=True)
    video_url, pid = cloudinary_upload(meta["file"])
    try:
        c = _graph("POST", f"{ig_id}/media", data={
            "media_type": "REELS", "video_url": video_url, "share_to_feed": "true",
            "caption": build_description(meta, sheet_url, for_ig=True), "access_token": token})["id"]
        for _ in range(60):  # up to ~10 min of processing
            st = _graph("GET", c, params={"fields": "status_code,status", "access_token": token})
            if st.get("status_code") == "FINISHED":
                break
            if st.get("status_code") in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"Reel processing failed: {st}")
            time.sleep(10)
        else:
            raise RuntimeError("Reel processing timed out")
        media_id = _graph("POST", f"{ig_id}/media_publish", data={"creation_id": c, "access_token": token})["id"]
        info = _graph("GET", media_id, params={"fields": "permalink", "access_token": token})
        return media_id, info.get("permalink", f"https://www.instagram.com/{media_id}")
    finally:
        cloudinary_delete(pid)


# ----------------------------------------------------------------------------- sheet back-fill
def backfill(links: dict[str, str], thread_links: dict[str, str]) -> None:
    """Write video URLs into News_Log.Related_Video_URL and Topic_Index.Latest_Video_URL (one batch each)."""
    from gspread.utils import rowcol_to_a1

    sh = open_spreadsheet()
    ws_n, ws_t = sh.worksheet(NEWS_SHEET), sh.worksheet(TOPIC_SHEET)
    col_n = ws_n.row_values(1).index("Related_Video_URL") + 1
    col_t = ws_t.row_values(1).index("Latest_Video_URL") + 1
    n_updates = [{"range": rowcol_to_a1(i + 1, col_n), "values": [[links[nid]]]}
                 for i, nid in enumerate(ws_n.col_values(1)) if nid in links]
    t_updates = [{"range": rowcol_to_a1(i + 1, col_t), "values": [[thread_links[tid]]]}
                 for i, tid in enumerate(ws_t.col_values(1)) if tid in thread_links]
    if n_updates:
        ws_n.batch_update(n_updates, value_input_option="RAW")
    if t_updates:
        ws_t.batch_update(t_updates, value_input_option="RAW")


def main() -> int:
    mpath = OUT / "manifest.json"
    manifest = json.loads(mpath.read_text())
    if not manifest["videos"]:
        print("[info] no videos to upload")
        return 0
    records = state_store.load()
    links: dict[str, str] = {}
    thread_links: dict[str, str] = {}
    failures = 0
    for meta in manifest["videos"]:
        rec = {
            "published_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "title": meta["title"], "file": os.path.relpath(meta["file"], ROOT),
            "news_ids": [i["news_id"] for i in meta["items"]],
            "thread_ids": sorted({i["thread_id"] for i in meta["items"]}),
            "youtube_id": "", "youtube_url": "", "instagram_id": "", "instagram_url": "",
        }

        def keep(rec=rec):  # persist immediately so a crash can never lose a published video's ID
            if rec not in records:
                records.append(rec)
            state_store.save(records)

        if DO_YT and not meta.get("youtube_url"):
            try:
                rec["youtube_id"] = yt_upload(meta, manifest["sheet_url"])
                rec["youtube_url"] = meta["youtube_url"] = f"https://www.youtube.com/shorts/{rec['youtube_id']}"
                keep()
                print(f"[ok] YouTube: {rec['youtube_url']}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"[error] YouTube upload failed: {e}")
        if DO_IG and not meta.get("instagram_url"):
            try:
                rec["instagram_id"], rec["instagram_url"] = ig_publish(meta, manifest["sheet_url"])
                meta["instagram_url"] = rec["instagram_url"]
                keep()
                print(f"[ok] Instagram: {rec['instagram_url']}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"[error] Instagram publish failed: {e}")
        best = rec["youtube_url"] or rec["instagram_url"]
        if best:
            for it in meta["items"]:
                links[it["news_id"]] = best
                thread_links[it["thread_id"]] = best
        mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    if links:
        backfill(links, thread_links)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
