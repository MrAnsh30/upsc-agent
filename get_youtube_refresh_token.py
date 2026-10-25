#!/usr/bin/env python3
"""ONE-TIME, run on your own laptop (needs a browser). Prints a refresh token for the YT_REFRESH_TOKEN secret.

1. Google Cloud Console -> create project -> enable "YouTube Data API v3".
2. OAuth consent screen: set user type External, add yourself as test user, then click
   "PUBLISH APP" (In production). While in 'Testing' mode refresh tokens expire after 7 days.
3. Credentials -> Create OAuth client ID -> Desktop app -> download JSON as client_secret.json
4. pip install google-auth-oauthlib && python get_youtube_refresh_token.py
"""
from google_auth_oauthlib.flow import InstalledAppFlow

flow = InstalledAppFlow.from_client_secrets_file(
    "client_secret.json", ["https://www.googleapis.com/auth/youtube.upload"])
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
print("\nYT_CLIENT_ID     =", creds.client_id)
print("YT_CLIENT_SECRET =", creds.client_secret)
print("YT_REFRESH_TOKEN =", creds.refresh_token)
