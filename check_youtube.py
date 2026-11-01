#!/usr/bin/env python3
"""Read-only YouTube connection test. Uploads nothing, deletes nothing, prints no secrets."""
import os
import sys

NAMES = ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")
vals = {n: os.environ.get(n, "") for n in NAMES}
ok = True
for n, v in vals.items():
    if not v:
        print(f"[problem] secret {n} is empty or missing in GitHub"); ok = False
    elif v != v.strip():
        print(f"[problem] secret {n} has a space or new line at the start/end - re-save it without extra spaces"); ok = False
    elif v[0] in "\"'" or v[-1] in "\"'":
        print(f"[problem] secret {n} starts or ends with a quote mark - re-save it WITHOUT quotes"); ok = False
if vals["YT_REFRESH_TOKEN"] and not vals["YT_REFRESH_TOKEN"].strip("\"' ").startswith("1//"):
    print("[problem] YT_REFRESH_TOKEN should begin with 1//  (copy it from the 'Refresh token' box in Step 2 of the Playground)"); ok = False
if not ok:
    sys.exit(1)

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

creds = Credentials(None, refresh_token=vals["YT_REFRESH_TOKEN"], token_uri="https://oauth2.googleapis.com/token",
                    client_id=vals["YT_CLIENT_ID"], client_secret=vals["YT_CLIENT_SECRET"],
                    scopes=["https://www.googleapis.com/auth/youtube"])
try:
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    items = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
except RefreshError as e:
    print(f"[problem] Google rejected the token: {e}\n"
          "  -> the Client ID, Client secret and Refresh token must all come from the SAME OAuth client, and the Playground "
          "must have been set to 'Use your own OAuth credentials' with Access type = Offline. Create a new token and try again.")
    sys.exit(1)
if not items:
    print("[problem] Connected, but this Google account has no YouTube channel. Create one at youtube.com first.")
    sys.exit(1)
print(f"OK - connected to YouTube channel: {items[0]['snippet']['title']}")
print("Upload and delete permission (youtube scope) is in place.")
