"""Shared helpers: env config, Google Sheets access, free-tier Gemini JSON calls, sheet schema."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
(OUT / "videos").mkdir(parents=True, exist_ok=True)
IST = ZoneInfo("Asia/Kolkata")

TOPIC_SHEET = "Topic_Index"   # one row per topic thread (e.g. "Russia-Ukraine War")
NEWS_SHEET = "News_Log"       # one row per news item, linked to a thread

TOPIC_HEADERS = [
    "Thread_ID", "Thread_Title", "Category", "GS_Paper", "Syllabus_Mapping", "Type",
    "Status", "Start_Date", "Last_Update", "End_Date", "Expiry_Date", "Update_Count",
    "Running_Log", "Latest_Video_URL",
]
NEWS_HEADERS = [
    "News_ID", "Date", "Thread_ID", "Topic", "Sub_Topic", "Headline", "Summary",
    "Key_Points", "Prelims_Facts", "GS_Paper", "Syllabus_Mapping", "Source",
    "Source_URL", "Related_Video_URL", "Added_On",
]


def env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise SystemExit(f"Missing required environment variable: {name}")
    return val or ""


def today_ist() -> dt.date:
    return dt.datetime.now(IST).date()


def sha(s: str, n: int = 12) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:n]


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ----------------------------------------------------------------------------- Sheets
def open_spreadsheet():
    import gspread
    from google.oauth2.service_account import Credentials

    info = json.loads(env("GOOGLE_SERVICE_ACCOUNT_JSON", required=True))
    creds = Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"],
    )
    return gspread.authorize(creds).open_by_key(env("SHEET_ID", required=True))


def sheet_url(sh) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sh.id}/edit?usp=sharing"


def ensure_ws(sh, title: str, headers: list[str]):
    import gspread

    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=2000, cols=len(headers))
    if ws.row_values(1) != headers:
        ws.update(values=[headers], range_name="A1")
        ws.freeze(rows=1)
    return ws


def delete_rows(sh, ws, row_numbers: list[int]) -> None:
    """Delete many 1-indexed rows with as few API calls as possible (contiguous ranges, bottom-up)."""
    rows = sorted(set(row_numbers), reverse=True)
    ranges: list[list[int]] = []
    for r in rows:
        if ranges and r == ranges[-1][0] - 1:
            ranges[-1][0] = r
        else:
            ranges.append([r, r])
    reqs = [{"deleteDimension": {"range": {
        "sheetId": ws.id, "dimension": "ROWS", "startIndex": a - 1, "endIndex": b}}}
        for a, b in ranges]
    for i in range(0, len(reqs), 200):
        sh.batch_update({"requests": reqs[i:i + 200]})


# ----------------------------------------------------------------------------- LLM (Gemini free tier)
def llm_json(prompt: str, retries: int = 4):
    """Call Gemini (free tier) over REST and return parsed JSON. Logs every failure."""
    key = env("GEMINI_API_KEY", required=True)
    model = env("GEMINI_MODEL", "gemini-2.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
    }
    last: Exception | None = None
    for attempt in range(retries):
        try:
            print(f"[llm] calling {model} (attempt {attempt + 1})", flush=True)
            r = requests.post(url, headers={"x-goog-api-key": key}, json=body, timeout=90)
            if r.status_code != 200:
                print(f"[llm] HTTP {r.status_code}: {r.text[:300]}", flush=True)
                last = RuntimeError(f"HTTP {r.status_code}")
                if r.status_code in (400, 401, 403, 404):
                    break  # a wrong key/model name will not fix itself
                time.sleep(20 * (attempt + 1))
                continue
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
            data = json.loads(text)
            time.sleep(float(env("LLM_PACE_SECONDS", "7")))
            return data
        except Exception as e:  # noqa: BLE001
            print(f"[llm] error: {e}", flush=True)
            last = e
            time.sleep(5)
    raise RuntimeError(f"LLM call failed after {retries} attempts: {last}")
