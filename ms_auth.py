#!/usr/bin/env python3
"""
ms_auth.py — one-time Microsoft OAuth setup.
Run this once on the VPS to get a token. The token is saved to ms_token.json
and refreshed automatically by briefing.py each morning.
"""

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
ENV_PATH   = SCRIPT_DIR / ".env"
TOKEN_PATH = SCRIPT_DIR / "ms_token.json"

SCOPES = "Calendars.Read User.Read offline_access"
AUTH_URL  = "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"


def load_env():
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def get_token_from_code(code: str, client_id: str, client_secret: str, redirect_uri: str) -> dict:
    data = urllib.parse.urlencode({
        "client_id":     client_id,
        "client_secret": client_secret,
        "code":          code,
        "redirect_uri":  redirect_uri,
        "grant_type":    "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main():
    load_env()
    client_id     = os.environ["MS_CLIENT_ID"]
    client_secret = os.environ["MS_CLIENT_SECRET"]
    redirect_uri  = os.environ["MS_REDIRECT_URI"]

    # Build auth URL
    params = urllib.parse.urlencode({
        "client_id":     client_id,
        "response_type": "code",
        "redirect_uri":  redirect_uri,
        "scope":         SCOPES,
        "response_mode": "query",
    })
    url = f"{AUTH_URL}?{params}"

    print("\n── Microsoft OAuth Setup ──────────────────────────────")
    print("\n1. Open this URL in your browser:\n")
    print(url)
    print("\n2. Sign in with your Outlook account and approve access.")
    print("3. You'll be redirected to http://localhost:8080/?code=...")
    print("   (the page won't load — that's fine)")
    print("4. Copy the full URL from the browser address bar and paste it below.\n")

    redirected = input("Paste the full redirect URL here: ").strip()

    # Extract code from URL
    parsed = urllib.parse.urlparse(redirected)
    code   = urllib.parse.parse_qs(parsed.query).get("code", [None])[0]
    if not code:
        print("ERROR: Could not find 'code' in the URL. Try again.")
        return

    print("\nExchanging code for token...")
    token = get_token_from_code(code, client_id, client_secret, redirect_uri)

    if "error" in token:
        print(f"ERROR: {token['error']} — {token.get('error_description', '')}")
        return

    TOKEN_PATH.write_text(json.dumps(token, indent=2))
    print(f"\n✓ Token saved to {TOKEN_PATH}")
    print("  You can now run briefing.py — it will refresh this token automatically.")


if __name__ == "__main__":
    main()
