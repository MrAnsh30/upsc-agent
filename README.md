# UPSC Daily Current-Affairs Agent — $0 Autonomous Pipeline

RSS news → free LLM triage/summaries/clustering → Master Google Sheet → narrated vertical videos →
YouTube Shorts + Instagram Reels, every day, on GitHub Actions. No paid service anywhere.

> **Read section 9 first.** Four things cannot be made fully "zero-touch" or are legally sensitive
> (YouTube audit, Instagram account type, news copyright, one-time OAuth consent).

---

## 1. Architecture & data flow

```
                       GitHub Actions cron (00:30 UTC = 06:00 IST)
                                       │
┌──────────────────────────────────────▼───────────────────────────────────────┐
│ STAGE 1  fetch_and_cluster.py                                                │
│                                                                              │
│  RSS: The Hindu · Indian Express · PIB ──► feedparser ──► dedupe (hash/title)│
│                                              │                               │
│                    Gemini free tier  ◄───────┘                               │
│        (a) TRIAGE  : pick ≤20 UPSC-relevant items                            │
│        (b) ENRICH  : own-words summary, key points, prelims facts,           │
│                      GS paper + syllabus mapping, image query,               │
│                      assign to EXISTING thread or create NEW thread,         │
│                      flag "is_concluded"                                     │
│        (c) LIFECYCLE: auto-close stale threads, purge expired (retention.py) │
│                                              │                               │
│                                              ▼                               │
│                    MASTER GOOGLE SHEET  (gspread + service account)          │
│                    ├─ Topic_Index  (1 row / thread, running log, expiry)     │
│                    └─ News_Log     (1 row / news item)                       │
│                                              │                               │
│                           output/bundle.json (only NEW items)                │
└──────────────────────────────────────┬───────────────────────────────────────┘
┌──────────────────────────────────────▼───────────────────────────────────────┐
│ STAGE 2  video_generator.py                                                  │
│  split into ⌈n/5⌉ videos (max 5) ─► Gemini writes ≤55 s script per video     │
│  ─► edge-tts voice per scene ─► images: Wikimedia Commons → Pexels →         │
│     Unsplash → generated gradient ─► Pillow text overlays ─► MoviePy         │
│     (1080×1920, pan effect, dip-to-black transitions) ─► output/videos/*.mp4 │
└──────────────────────────────────────┬───────────────────────────────────────┘
┌──────────────────────────────────────▼───────────────────────────────────────┐
│ STAGE 3  uploader.py                                                         │
│  YouTube Data API v3 (resumable upload, "#Shorts", Sheet URL in description) │
│  Cloudinary (temp public URL) ─► Instagram Graph API Reels (Sheet URL caption)│
│  ─► write video URLs back into News_Log + Topic_Index                        │
└──────────────────────────────────────────────────────────────────────────────┘
```

## 2. Free tech stack

| Layer | Tool | Why it is $0 |
|---|---|---|
| Scheduler / compute | GitHub Actions | Unlimited minutes on public repos; 2,000 min/month on private repos |
| News | RSS (The Hindu, Indian Express, PIB) via `feedparser` | Public feeds, no key |
| Article context | `trafilatura` | Open source (context for the LLM only, never republished) |
| LLM (triage, summaries, clustering, scripts) | Google Gemini API free tier (REST) | Free key from AI Studio; rate-limited (pacing built in) |
| Database | Google Sheets API + `gspread` | Free; service account |
| Voice-over | `edge-tts` (fallback `gTTS`) | Open-source clients of free endpoints |
| Images | Wikimedia Commons API (no key), Pexels API, Unsplash API | Free keys / no key |
| Video | MoviePy + FFmpeg + Pillow + NumPy | Open source |
| Publishing | YouTube Data API v3; Instagram Graph API | Free (quota-limited) |
| Public URL for Reels | Cloudinary free plan (file deleted after publish) | Free tier |

## 3. Master Sheet design

**`Topic_Index`** (one row per *thread* — this is the clustering unit)

| Column | Purpose |
|---|---|
| Thread_ID | `russia-ukraine-war-6889` (slug + hash) |
| Thread_Title | Durable, broad title (LLM-enforced) |
| Category / GS_Paper / Syllabus_Mapping | UPSC mapping |
| Type | `LONG_RUNNING` (wars, crises) or `STANDARD` |
| Status | `ONGOING` / `ENDED` |
| Start_Date / Last_Update / End_Date | Lifecycle dates (ISO) |
| Expiry_Date | ISO date, or `RETAINED (ongoing)` |
| Update_Count | Number of items in thread |
| Running_Log | Dated one-line timeline (`[2026-10-24] headline` …), oldest trimmed past 40k chars |
| Latest_Video_URL | Latest video for this thread |

**`News_Log`** (one row per item): `News_ID, Date, Thread_ID, Topic, Sub_Topic, Headline, Summary, Key_Points, Prelims_Facts, GS_Paper, Syllabus_Mapping, Source, Source_URL, Related_Video_URL, Added_On`.

Students filter `News_Log` by `Thread_ID` (or `Topic`/`GS_Paper`) or read the `Running_Log` cell in `Topic_Index` for the whole story in one place. 5 years × ~20 items/day ≈ 36k rows × 15 cols ≈ 550k cells, well under Sheets' 10M-cell cap.

### Clustering logic
1. Gemini sees today's items **plus the list of active threads** (`id | title | type`, up to 250 most recent) and must reuse an existing `thread_id` when the story continues, otherwise propose a broad new title.
2. Items are processed in batches of 6; new threads are written into memory after each batch so later batches reuse them (no duplicates like "Russia–Ukraine War" vs "Ukraine conflict").
3. Fresh news on an `ENDED` thread **reopens** it (`ONGOING`, End_Date cleared).

### Retention algorithm (`retention.py`, unit-tested)
```python
def expiry_date(start, status, end):
    if status != "ENDED" or end is None:
        return None                                  # ongoing → NEVER purge (even after 5 years)
    return max(start + 5 years, end + 2 years)       # default 5y; post-end extension 2y

def daily_maintenance(thread, today):
    if thread.status == "ONGOING" and idle_days(thread) > {"STANDARD": 90, "LONG_RUNNING": 365}[thread.type]:
        thread.status, thread.end = "ENDED", thread.last_update      # safety valve
    if expiry_date(...) and today >= expiry_date(...):
        delete all News_Log rows with this Thread_ID, then the Topic_Index row   # logged to purge_log.json
```

| Scenario | Result |
|---|---|
| One-off news on 10 Jan 2026 | Auto-closed after 90 idle days → purged **10 Jan 2031** |
| Russia–Ukraine War (start 24 Feb 2022), still ongoing in 2027 | **Never purged** while ongoing |
| …concludes 1 Jun 2027 | `max(24 Feb 2027, 1 Jun 2029)` → purged **1 Jun 2029** (7y 3m total) |
| Event Jan 2024 – Feb 2024 | `max(Jan 2029, Feb 2026)` → **Jan 2029** (default 5y) |

*Interpretation note:* "extend by 2 years once concluded" is implemented as `end + 2y` when that is later than `start + 5y`. If you instead want a flat `+2y` on top of 5y for every ended thread, change the one `max(...)` line in `retention.expiry_date`.

## 4. Files

| File | Role |
|---|---|
| `fetch_and_cluster.py` | Script 1 — RSS, Gemini, Sheets, purge |
| `video_generator.py` | Script 2 — scripts, edge-tts, images, MoviePy |
| `uploader.py` | Script 3 — YouTube, Instagram, sheet back-fill |
| `retention.py` | Lifecycle rules (`python retention.py` runs self-tests) |
| `common.py` | Env, Sheets helpers, Gemini JSON client, schema |
| `get_youtube_refresh_token.py` | One-time local helper |
| `.github/workflows/daily.yml` | Daily cron pipeline |

## 5. Setup guide (≈ 60–90 minutes, once)

**A. Repo.** Create a GitHub repo (public = unlimited free minutes) and push this folder. Keep secrets only in *Settings → Secrets and variables → Actions*.

**B. Google Sheet + service account.**
1. Cloud Console → new project → enable **Google Sheets API** and **Google Drive API**.
2. IAM → Service Accounts → create → Keys → add JSON key. Paste the *entire JSON* into secret `GOOGLE_SERVICE_ACCOUNT_JSON`.
3. Create a blank Google Sheet, share it with the service account's e-mail as **Editor**, copy the ID from its URL into secret `SHEET_ID`.
4. Set *General access → Anyone with the link → Viewer* (the script also tries this automatically). The sheet URL is what goes into video descriptions.

**C. Gemini.** Get a free key at Google AI Studio → secret `GEMINI_API_KEY`. (Optional variable `GEMINI_MODEL`; defaults to `gemini-2.5-flash` — change it if Google retires that name.)

**D. Images.** Wikimedia needs nothing. Add free keys `PEXELS_API_KEY` (and optionally `UNSPLASH_ACCESS_KEY`). Put your contact e-mail in `UA` in `video_generator.py` (Wikimedia asks for it).

**E. YouTube.** Follow the docstring in `get_youtube_refresh_token.py` (publish the OAuth app to *In production* or the token dies after 7 days), run it locally, store `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN`. Start with `YT_PRIVACY: private` in the workflow until the audit (section 9) is approved.

**F. Instagram.** Convert the account to *Professional (Creator/Business)* → link it to a Facebook Page → create a Meta developer app with Instagram Graph API permissions (`instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement`) → generate a **long-lived** token → secrets `IG_USER_ID`, `IG_ACCESS_TOKEN`. Set your bio link to the Master Sheet (captions are not clickable).

**G. Cloudinary.** Free account → secrets `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`.

**H. Dry run.** Actions tab → *UPSC daily agent* → **Run workflow**. Check: Sheet tabs created, rows appended, MP4s in the run artifacts. Test uploads with `YT_PRIVACY=private` first; set `UPLOAD_IG: "0"` in the workflow env to test YouTube alone.

**I. Go live.** Leave it. The cron runs daily; the keep-alive commit prevents GitHub from pausing the schedule after 60 inactive days.

## 6. Local test

```bash
pip install -r requirements.txt && sudo apt install ffmpeg fonts-dejavu-core
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat sa.json)" SHEET_ID=... GEMINI_API_KEY=... PEXELS_API_KEY=...
python fetch_and_cluster.py && python video_generator.py
UPLOAD_YT=0 UPLOAD_IG=0 python uploader.py
```

## 7. Tuning knobs (env vars)
`MAX_ITEMS` (20), `ITEMS_PER_VIDEO` (5), `MAX_VIDEOS` (5), `LOOKBACK_HOURS` (30), `TTS_VOICE` (`en-IN-NeerjaNeural`; Hindi: `hi-IN-SwaraNeural`), `TTS_RATE`, `GEMINI_MODEL`, `LLM_PACE_SECONDS`, `YT_PRIVACY`, `UPLOAD_YT`, `UPLOAD_IG`, `GRAPH_VERSION`.

## 8. Dynamic video splitting
`n_videos = min(MAX_VIDEOS, ceil(items / 5))`; items are sorted by category/GS paper so each video is thematically coherent. Voice-over longer than 58 s is automatically re-recorded faster. Items beyond `MAX_VIDEOS × ITEMS_PER_VIDEO` still go to the Sheet, just not into a video.

## 9. Honest limitations (please read)

| Issue | Reality | What to do |
|---|---|---|
| **YouTube API audit** | Videos uploaded through an *unverified* API project are locked to **private** until Google audits the project. | Apply via YouTube's API Compliance Audit form (free, takes days–weeks). Until then, uploads work but stay private. |
| **YouTube quota** | 10,000 units/day; an upload costs ~1,600 → about 6 uploads/day. | `MAX_VIDEOS=5` stays inside it. |
| **Instagram prerequisites** | Needs a Professional account + Facebook Page + Meta app. Reels publishing is rate-limited (≈100 API posts/24h). Graph API versions expire periodically. | Bump `GRAPH_VERSION` when Meta announces a deprecation; long-lived tokens still need occasional refresh—monitor failures. |
| **News copyright** | Hindu / Indian Express text and photos are copyrighted. The pipeline uses them only as *input*, writes original-wording summaries, links the source, and never uses their photos (Wikimedia/Pexels/Unsplash only, credits added to descriptions). | Keep summaries short and transformative; PIB (government) is the safest source. Get legal advice before monetising. |
| **Feed access** | Some publishers/PIB block cloud IPs or change feed URLs. | Failed feeds are skipped with a warning; edit `FEEDS`. If PIB blocks GitHub IPs, add alternate feeds. |
| **LLM accuracy** | Free LLMs can mis-summarise or mis-judge "concluded". | Spot-check the Sheet weekly. Conclusion detection is deliberately conservative (ceasefire ≠ end). |
| **"Zero human intervention"** | One-time OAuth consent and Meta setup are unavoidable; tokens occasionally need renewal. | Treat failed Actions runs as the alert (GitHub e-mails you). |
| **Free-tier limits change** | Gemini/Pexels/Cloudinary/Actions limits and model names change. | Check current limits; the pacing/model name are env-configurable. |
| **Back-ups** | Purges are permanent. | `output/purge_log.json` is saved as an Actions artifact; use *File → Version history* in Sheets for rollback. |
