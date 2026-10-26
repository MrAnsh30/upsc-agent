#!/usr/bin/env python3
"""Script 1 — Fetch -> triage -> summarise & cluster -> update Master Google Sheet -> purge expired.

Writes output/bundle.json for the video stage (only NEW items from this run).
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import sys

import feedparser
import requests

import retention
from common import (NEWS_HEADERS, NEWS_SHEET, OUT, TOPIC_HEADERS, TOPIC_SHEET, delete_rows,
                    ensure_ws, env, llm_json, open_spreadsheet, sha, sheet_url, slugify,
                    today_ist)

# Verify these URLs once; feeds occasionally move. Failed feeds are skipped, not fatal.
FEEDS = [
    ("The Hindu", "https://www.thehindu.com/news/national/feeder/default.rss"),
    ("The Hindu", "https://www.thehindu.com/news/international/feeder/default.rss"),
    ("The Hindu", "https://www.thehindu.com/business/Economy/feeder/default.rss"),
    ("The Hindu", "https://www.thehindu.com/sci-tech/science/feeder/default.rss"),
    ("The Hindu", "https://www.thehindu.com/opinion/editorial/feeder/default.rss"),
    ("Indian Express", "https://indianexpress.com/section/india/feed/"),
    ("Indian Express", "https://indianexpress.com/section/world/feed/"),
    ("Indian Express", "https://indianexpress.com/section/explained/feed/"),
    ("PIB", "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3"),
]
UA = {"User-Agent": "Mozilla/5.0 (compatible; UPSC-CA-Agent/1.0)"}
LOOKBACK_HOURS = int(env("LOOKBACK_HOURS", "30"))
MAX_ITEMS = int(env("MAX_ITEMS", "20"))
BATCH = 6
RUNNING_LOG_CAP = 40_000  # Google Sheets cell limit is 50,000 chars

CATEGORIES = ["Polity & Governance", "Economy", "International Relations", "Environment & Ecology",
              "Science & Technology", "Internal Security & Defence", "Social Issues & Welfare",
              "History, Art & Culture", "Geography", "Ethics & Misc"]


# ----------------------------------------------------------------------------- fetch
def clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def fetch_articles() -> list[dict]:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=LOOKBACK_HOURS)
    arts, seen = [], set()
    for source, url in FEEDS:
        try:
            r = requests.get(url, headers=UA, timeout=25)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] feed failed {url}: {e}")
            continue
        for e in feed.entries:
            link, title = (e.get("link") or "").strip(), clean(e.get("title", ""))
            if not link or not title:
                continue
            ts = e.get("published_parsed") or e.get("updated_parsed")
            if ts and dt.datetime(*ts[:6], tzinfo=dt.timezone.utc) < cutoff:
                continue
            key = re.sub(r"[^a-z0-9]", "", title.lower())
            if key in seen:
                continue
            seen.add(key)
            arts.append({"id": sha(link), "source": source, "title": title, "link": link,
                         "snippet": clean(e.get("summary", ""))[:500]})
    print(f"[info] fetched {len(arts)} fresh articles")
    return arts


def interleave(arts: list[dict]) -> list[dict]:
    """Round-robin across sources so one noisy feed can't crowd out PIB / editorials."""
    by_src: dict[str, list[dict]] = {}
    for a in arts:
        by_src.setdefault(a["source"], []).append(a)
    out, i = [], 0
        while i < max(len(v) for v in by_src.values()):
        for lst in by_src.values():
            if i < len(lst):
                out.append(lst[i])
        i += 1
    return out


def article_text(url: str) -> str:
    """Context for the LLM only. We never republish this text."""
    try:
        import trafilatura
        r = requests.get(url, headers=UA, timeout=20)
        return (trafilatura.extract(r.text, include_comments=False) or "")[:1500]
    except Exception:  # noqa: BLE001
        return ""


# ----------------------------------------------------------------------------- LLM stages
def triage(arts: list[dict]) -> list[dict]:
    pool = interleave(arts)[:150]
    lines = "\n".join(f"{i}|{a['source']}|{a['title']}|{a['snippet'][:140]}" for i, a in enumerate(pool))
    prompt = f"""You are a UPSC CSE current-affairs editor. From the list below pick the items most useful
for UPSC Prelims + Mains (GS1-4, Essay). Skip sports results, crime, celebrity news, routine stock moves,
local-politics trivia, advertorials. Prefer policy, governance, schemes, international relations, economy
data/reforms, environment, science & tech, security, court judgments, reports/indices, analytical editorials.
Pick only one item per event.
Return JSON: {{"selected":[{{"i":<index>,"score":<1-10>}}]}} with at most {MAX_ITEMS} items, score >= 6 only.

ITEMS (index|source|title|snippet):
{lines}"""
    res = llm_json(prompt)
    sel = sorted(res.get("selected", []), key=lambda x: -int(x.get("score", 0)))[:MAX_ITEMS]
    return [pool[s["i"]] for s in sel if isinstance(s.get("i"), int) and 0 <= s["i"] < len(pool)]


def enrich_batch(batch: list[dict], threads: dict[str, dict]) -> list[dict]:
    active = sorted(
        (t for t in threads.values()
         if t["Status"] == "ONGOING" or (today_ist() - (retention.parse(t["Last_Update"]) or today_ist())).days < 120),
        key=lambda t: t["Last_Update"], reverse=True)[:250]
    thread_list = "\n".join(f"{t['Thread_ID']} | {t['Thread_Title']} | {t['Type']}" for t in active) or "(none yet)"
    arts = "\n\n".join(
        f"[{i}] SOURCE: {a['source']}\nTITLE: {a['title']}\nSNIPPET: {a['snippet']}\nTEXT: {a.get('text', '')}"
        for i, a in enumerate(batch))
    prompt = f"""You build UPSC revision notes. For each article decide if it is exam-relevant and produce notes
in YOUR OWN WORDS (never copy sentences from the source).

RULES
- Reuse an EXISTING thread if the article continues the same story/conflict/policy/scheme/issue. Thread titles
  must be broad and durable (e.g. "Russia-Ukraine War", "Manipur Ethnic Violence", "GST Rate Rationalisation"),
  never date-specific.
- If no existing thread fits, set thread_id=null and give new_thread_title.
- thread_type: LONG_RUNNING for wars, conflicts, crises, multi-month negotiations/sagas; else STANDARD.
- is_concluded=true ONLY for a definitive end (final peace treaty signed, law fully repealed/implemented,
  event completed). A ceasefire, pause or talks is NOT conclusion.
- category must be one of: {json.dumps(CATEGORIES)}
- gs_paper one of GS1, GS2, GS3, GS4, Prelims. syllabus_mapping like "GS2 - International Relations: India and neighbourhood".
- image_query: 3-5 generic words for a stock/Wikimedia photo (e.g. "Parliament of India building").

EXISTING ACTIVE THREADS (id | title | type):
{thread_list}

ARTICLES:
{arts}

Return JSON: {{"items":[{{"i":0,"keep":true,"thread_id":null,"new_thread_title":"","thread_type":"STANDARD",
"category":"","sub_topic":"","headline":"<=90 chars","summary":"60-80 words, UPSC angle","key_points":["","",""],
"prelims_facts":[],"gs_paper":"","syllabus_mapping":"","is_concluded":false,"image_query":""}}]}}"""
    res = llm_json(prompt)
    out = []
    for it in res.get("items", []):
        i = it.get("i")
        if isinstance(i, int) and 0 <= i < len(batch) and it.get("keep") and it.get("headline") and it.get("summary"):
            it["_art"] = batch[i]
            out.append(it)
    return out


# ----------------------------------------------------------------------------- sheet <-> dict helpers
def read_threads(ws) -> dict[str, dict]:
    vals = ws.get_all_values()
    threads = {}
    for row in vals[1:]:
        row = row + [""] * (len(TOPIC_HEADERS) - len(row))
        t = dict(zip(TOPIC_HEADERS, row))
        if t["Thread_ID"]:
            t["Update_Count"] = int(t["Update_Count"] or 0)
            threads[t["Thread_ID"]] = t
    return threads


def write_threads(ws, threads: dict[str, dict]) -> None:
    rows = [[t[h] for h in TOPIC_HEADERS] for t in threads.values()]
    if rows:
        ws.update(values=rows, range_name="A2", value_input_option="RAW")


def refresh_expiry(t: dict) -> None:
    start = retention.parse(t["Start_Date"]) or today_ist()
    t["Expiry_Date"] = retention.expiry_label(start, t["Status"], retention.parse(t["End_Date"]))


def upsert(items: list[dict], threads: dict[str, dict], today: dt.date) -> tuple[list[list], list[dict]]:
    news_rows, bundle = [], []
    iso = today.isoformat()
    for it in items:
        a = it["_art"]
        tid = it.get("thread_id")
        if tid not in threads:
            title = (it.get("new_thread_title") or it["headline"]).strip()
            tid = f"{slugify(title)[:40]}-{sha(title, 4)}"
            if tid not in threads:
                threads[tid] = {
                    "Thread_ID": tid, "Thread_Title": title, "Category": it.get("category", ""),
                    "GS_Paper": it.get("gs_paper", ""), "Syllabus_Mapping": it.get("syllabus_mapping", ""),
                    "Type": "LONG_RUNNING" if it.get("thread_type") == "LONG_RUNNING" else "STANDARD",
                    "Status": "ONGOING", "Start_Date": iso, "Last_Update": iso, "End_Date": "",
                    "Expiry_Date": "", "Update_Count": 0, "Running_Log": "", "Latest_Video_URL": ""}
        t = threads[tid]
        line = f"[{iso}] {it['headline']}"
        log = (t["Running_Log"] + "\n" + line).strip()
        while len(log) > RUNNING_LOG_CAP:  # drop oldest lines
            log = log.split("\n", 1)[1]
        t.update(Running_Log=log, Last_Update=iso, Update_Count=t["Update_Count"] + 1)
        if it.get("is_concluded"):
            t.update(Status="ENDED", End_Date=iso)
        elif t["Status"] == "ENDED":  # fresh development -> reopen
            t.update(Status="ONGOING", End_Date="")
        refresh_expiry(t)

        kp = " | ".join(it.get("key_points", []))
        pf = " | ".join(it.get("prelims_facts", []))
        news_rows.append([a["id"], iso, tid, it.get("category", t["Category"]), it.get("sub_topic", ""),
                          it["headline"], it["summary"], kp, pf, it.get("gs_paper", ""),
                          it.get("syllabus_mapping", ""), a["source"], a["link"], "", iso])
        bundle.append({
            "news_id": a["id"], "thread_id": tid, "thread_title": t["Thread_Title"],
            "headline": it["headline"], "summary": it["summary"], "key_points": it.get("key_points", []),
            "prelims_facts": it.get("prelims_facts", []), "category": it.get("category", ""),
            "gs_paper": it.get("gs_paper", ""), "source": a["source"],
            "image_query": it.get("image_query") or it["headline"], "ongoing": t["Status"] == "ONGOING"})
    return news_rows, bundle


# ----------------------------------------------------------------------------- lifecycle maintenance
def maintain_and_purge(sh, ws_t, ws_n, today: dt.date) -> None:
    threads = read_threads(ws_t)
    changed = False
    for t in threads.values():  # auto-close stale threads
        last = retention.parse(t["Last_Update"]) or today
        if retention.should_autoclose(t["Type"], t["Status"], last, today):
            t.update(Status="ENDED", End_Date=last.isoformat())
            changed = True
        refresh_expiry(t)
    expired = {tid for tid, t in threads.items()
               if retention.should_purge(retention.parse(t["Start_Date"]) or today, t["Status"],
                                         retention.parse(t["End_Date"]), today)}
    if expired:
        news_thread_col = ws_n.col_values(3)  # Thread_ID column
        news_rows = [i + 1 for i, v in enumerate(news_thread_col) if i > 0 and v in expired]
        topic_rows = [i + 2 for i, tid in enumerate(threads) if tid in expired]
        (OUT / "purge_log.json").write_text(json.dumps(
            {"date": today.isoformat(), "threads": sorted(expired), "news_rows": len(news_rows)}, indent=2))
        if news_rows:
            delete_rows(sh, ws_n, news_rows)
        if topic_rows:
            delete_rows(sh, ws_t, topic_rows)
        print(f"[info] purged {len(expired)} expired threads, {len(news_rows)} news rows")
    elif changed:
        write_threads(ws_t, threads)


# ----------------------------------------------------------------------------- main
def main() -> int:
    today = today_ist()
    sh = open_spreadsheet()
    ws_t = ensure_ws(sh, TOPIC_SHEET, TOPIC_HEADERS)
    ws_n = ensure_ws(sh, NEWS_SHEET, NEWS_HEADERS)
    try:  # make the Master Sheet readable by students (needs owner/editor rights; harmless if it fails)
        sh.share(None, perm_type="anyone", role="reader")
    except Exception as e:  # noqa: BLE001
        print(f"[info] could not auto-share (set 'Anyone with link: Viewer' manually once): {e}")

    maintain_and_purge(sh, ws_t, ws_n, today)

    threads = read_threads(ws_t)
    known_ids = set(ws_n.col_values(1))
    arts = [a for a in fetch_articles() if a["id"] not in known_ids]
    bundle_path = OUT / "bundle.json"
    if not arts:
        bundle_path.write_text(json.dumps({"date": today.isoformat(), "sheet_url": sheet_url(sh), "items": []}))
        print("[info] nothing new")
        return 0

    chosen = triage(arts)
    print(f"[info] triage selected {len(chosen)} items")
    for a in chosen:
        a["text"] = article_text(a["link"])

    news_rows: list[list] = []
    bundle: list[dict] = []
    for i in range(0, len(chosen), BATCH):
        batch_items = enrich_batch(chosen[i:i + BATCH], threads)
        # upsert per batch so brand-new threads are visible to the next batch (no duplicate threads)
        rows, items = upsert(batch_items, threads, today)
        news_rows.extend(rows)
        bundle.extend(items)

    if news_rows:
        ws_n.append_rows(news_rows, value_input_option="RAW")
        write_threads(ws_t, threads)
    bundle_path.write_text(json.dumps({"date": today.isoformat(), "sheet_url": sheet_url(sh),
                                       "items": bundle}, ensure_ascii=False, indent=2))
    print(f"[done] {len(news_rows)} items added, {len(threads)} threads tracked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
