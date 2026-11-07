#!/usr/bin/env python3
"""Read-only Instagram connection test. Posts nothing, deletes nothing, prints no secrets."""
import os
import sys

import requests

uid = os.environ.get("IG_USER_ID", "")
tok = os.environ.get("IG_ACCESS_TOKEN", "")
ver = os.environ.get("GRAPH_VERSION", "v23.0")
ok = True
for name, v in (("IG_USER_ID", uid), ("IG_ACCESS_TOKEN", tok)):
    if not v:
        print(f"[problem] secret {name} is empty or missing in GitHub"); ok = False
    elif v != v.strip():
        print(f"[problem] secret {name} has a space or new line at the start/end - re-save it"); ok = False
    elif v[0] in "\"'" or v[-1] in "\"'":
        print(f"[problem] secret {name} starts or ends with a quote mark - re-save it WITHOUT quotes"); ok = False
if not ok:
    sys.exit(1)
if not uid.isdigit():
    print("[problem] IG_USER_ID must be digits only"); sys.exit(1)
print(f"IG_USER_ID: {len(uid)} digits, starts with {uid[:5]}"
      + ("" if uid.startswith("17841") else "   <-- Instagram business account numbers normally start with 17841; "
         "business_id (starts 1523...) is the portfolio, NOT the Instagram account"))

def check_cloudinary() -> int:
    names = ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET")
    cloud, key, secret = (os.environ.get(n, "") for n in names)
    missing = [n for n, v in zip(names, (cloud, key, secret)) if not v.strip()]
    if missing:
        print("[problem] These Cloudinary values arrived EMPTY: " + ", ".join(missing))
        if len(missing) == 3:
            print("  -> all three are empty. Either (a) the workflow file check-instagram.yml in .github/workflows is the OLD version "
                  "(open it and search for CLOUDINARY - it must be there), or (b) none of the three secrets exist under "
                  "Settings > Secrets and variables > Actions > Repository secrets.")
        else:
            print("  -> only these are empty, so add or correct exactly these secret names (capital letters, no spaces).")
        return 1
    for n, v in zip(names, (cloud, key, secret)):
        if v != v.strip() or v[:1] in "\"'" or v[-1:] in "\"'":
            print(f"[problem] {n} has a space, new line or quote mark at the start/end - re-save it cleanly")
            return 1
    r = requests.get(f"https://api.cloudinary.com/v1_1/{cloud}/ping", auth=(key, secret), timeout=60)
    ok = False
    try:
        ok = r.status_code == 200 and (r.json() or {}).get("status") == "ok"
    except ValueError:
        pass
    if ok:
        print("OK - Cloudinary credentials work (the agent can upload the temporary video).")
        return 0
    print(f"[problem] Cloudinary rejected the credentials (HTTP {r.status_code}). Check the cloud name, API key and secret.")
    return 1


errors = []
for host in ("graph.facebook.com", "graph.instagram.com"):
    base = f"https://{host}/{ver}"
    r = requests.get(f"{base}/{uid}", params={"fields": "username", "access_token": tok}, timeout=60)
    data = r.json() if r.content else {}
    if r.status_code == 200 and data.get("username"):
        print(f"OK - connected to Instagram account @{data['username']}  (API host that works: {host})")
        lim = requests.get(f"{base}/{uid}/content_publishing_limit", params={"fields": "quota_usage,config", "access_token": tok}, timeout=60)
        if lim.status_code == 200:
            print("Publishing permission looks OK (content_publishing_limit readable).")
        else:
            msg = (lim.json().get("error", {}) or {}).get("message", lim.text[:200])
            print(f"[warning] could not read the publishing limit - the token may lack instagram_content_publish: {msg}")
        sys.exit(check_cloudinary())
    errors.append((host, (data.get("error", {}) or {}).get("message", f"HTTP {r.status_code}")))
for host, msg in errors:
    print(f"[problem] {host}: {msg}")
print("-> Check: (1) IG_USER_ID is the selected_asset_id number (17841...), (2) the token was generated for the UPSC Agent app by the "
      "system user that owns this Instagram account, (3) the token has instagram_basic.")
sys.exit(1)
