#!/usr/bin/env python3
"""
email_triage.py — Daily inbox triage via Microsoft Graph API.
Moves emails to Archive or "Check to Delete", leaves actionable ones in Inbox.
Uses rules first, local Ollama model for borderline cases.
"""

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime

SCRIPT_DIR   = Path(__file__).parent
ENV_PATH     = SCRIPT_DIR / ".env"
TOKEN_PATH   = SCRIPT_DIR / "ms_token.json"

GRAPH_BASE   = "https://graph.microsoft.com/v1.0"
TOKEN_URL    = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
OLLAMA_URL   = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:72b"

CHECK_FOLDER = "Check to Delete"


def load_env():
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def get_token():
    load_env()
    token = json.loads(TOKEN_PATH.read_text())
    data = urllib.parse.urlencode({
        "client_id":     os.environ["MS_CLIENT_ID"],
        "client_secret": os.environ["MS_CLIENT_SECRET"],
        "refresh_token": token["refresh_token"],
        "grant_type":    "refresh_token",
        "scope":         "Calendars.Read User.Read Mail.Read Mail.ReadWrite offline_access",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as r:
        token = json.loads(r.read())
    TOKEN_PATH.write_text(json.dumps(token, indent=2))
    return token["access_token"]


def graph(token, method, path, body=None):
    url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read()) if r.length != 0 else None


def get_or_create_folder(token, name):
    result = graph(token, "GET", "/me/mailFolders?$top=100")
    for folder in result.get("value", []):
        if folder["displayName"].lower() == name.lower():
            return folder["id"]
    result = graph(token, "POST", "/me/mailFolders", {"displayName": name})
    return result["id"]


def get_inbox_messages(token):
    messages = []
    path = "/me/mailFolders/inbox/messages?$top=50&$select=id,subject,from,receivedDateTime,isRead,bodyPreview"
    while path:
        result = graph(token, "GET", path)
        messages.extend(result.get("value", []))
        path = result.get("@odata.nextLink")
    return messages


def move_message(token, msg_id, folder_id):
    graph(token, "POST", f"/me/messages/{msg_id}/move", {"destinationId": folder_id})


# ── RULES ─────────────────────────────────────────────────────────────────────

DELETE_SUBJECT = [
    "% off", "sale ends", "limited time", "special offer", "exclusive deal",
    "discount", "flash sale", "black friday", "cyber monday", "don't miss",
    "last chance", "act now", "free shipping", "promo code", "coupon",
]

DELETE_SENDER = [
    "mailchimp.com", "sendgrid.net", "constantcontact.com", "klaviyo.com",
    "hubspot.com", "marketo.com", "em.", "email.", "news.", "newsletter.",
    "noreply@", "no-reply@", "notifications@", "marketing@", "promo@",
]

ARCHIVE_SUBJECT = [
    "order confirmation", "order #", "your order", "receipt", "invoice",
    "payment confirmation", "payment received", "shipping confirmation",
    "your package", "has been delivered", "tracking number",
    "booking confirmation", "reservation confirmation", "e-ticket",
    "registration confirmed", "password reset", "verify your email",
    "welcome to", "your account", "your booking", "booking reference",
    "flight confirmation", "booking code", "itinerary", "your trip",
    "your reservation", "appointment confirmation", "your appointment",
    "reimbursement", "claim", "diagnosis", "medical", "prescription",
    "insurance", "policy", "your policy", "google play", "app store",
    "your invoice", "your receipt", "your statement", "your bill",
    "direct debit", "standing order", "bank statement",
    "holiday", "camp", "school", "kindergarten",
    "welcome", "your new account", "energy", "electricity", "gas",
    "ukvi", "visa", "passport", "government",
]


def rules_classify(subject, sender_email, preview):
    s = subject.lower()
    e = sender_email.lower()
    p = preview.lower()

    for kw in DELETE_SUBJECT:
        if kw in s:
            return "DELETE"
    for domain in DELETE_SENDER:
        if domain in e:
            return "DELETE"
    if "unsubscribe" in p or "unsubscribe" in s:
        return "DELETE"

    for kw in ARCHIVE_SUBJECT:
        if kw in s:
            return "ARCHIVE"

    return None


def ollama_classify(subject, sender, preview):
    prompt = f"""Classify this email. Reply with exactly one word only: INBOX, ARCHIVE, or DELETE.

INBOX = personal email, requires action or reply, customer service issue, official/government email, anything with unresolved issues, anything sent directly to the recipient
ARCHIVE = receipt, invoice, booking confirmation, medical document, insurance document, payment confirmation, bank statement, school/camp info, welcome email from a service, anything that might be needed later
DELETE = pure marketing, newsletter, promotion, advertisement, discount offer, mass-sent promotional email with no personal or transactional content

When in doubt, choose ARCHIVE over DELETE. Only choose DELETE if you are certain it is promotional/marketing with no useful content.

Sender: {sender}
Subject: {subject}
Preview: {preview[:200]}

One word answer:"""

    data = json.dumps({
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "stream":  False,
        "options": {"temperature": 0},
    }).encode()
    req = urllib.request.Request(OLLAMA_URL, data=data)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        response = json.loads(r.read())["response"].strip().upper()
    for word in ["INBOX", "ARCHIVE", "DELETE"]:
        if word in response:
            return word
    return "INBOX"


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main(dry_run=False):
    mode = "DRY RUN" if dry_run else "LIVE"
    print(f"── Email Triage {datetime.now().strftime('%Y-%m-%d %H:%M')} [{mode}] ──")

    token = get_token()

    print("Setting up folders...")
    check_id   = get_or_create_folder(token, CHECK_FOLDER)
    archive_id = graph(token, "GET", "/me/mailFolders/archive")["id"]
    print(f"  ✓ folders ready\n")

    print("Fetching inbox...")
    messages = get_inbox_messages(token)
    print(f"  {len(messages)} messages found\n")

    counts = {"inbox": 0, "archive": 0, "delete": 0, "errors": 0}
    inbox_subjects = []

    for msg in messages:
        subject      = msg.get("subject") or "(no subject)"
        sender_email = msg.get("from", {}).get("emailAddress", {}).get("address", "")
        preview      = msg.get("bodyPreview", "")
        msg_id       = msg["id"]

        try:
            decision = rules_classify(subject, sender_email, preview)
            method   = "rules"
            if decision is None:
                decision = ollama_classify(subject, sender_email, preview)
                method   = "AI"

            if decision == "DELETE":
                if not dry_run:
                    move_message(token, msg_id, check_id)
                counts["delete"] += 1
                print(f"  🗑  [{method}] {subject[:70]}")
            elif decision == "ARCHIVE":
                if not dry_run:
                    move_message(token, msg_id, archive_id)
                counts["archive"] += 1
                print(f"  📁  [{method}] {subject[:70]}")
            else:
                counts["inbox"] += 1
                inbox_subjects.append(f"    • {subject[:70]}")

        except Exception as e:
            counts["errors"] += 1
            print(f"  ✗ Error on '{subject[:40]}': {e}")

    print(f"\n── Results ──────────────────────────────────────────────")
    print(f"  Kept in inbox:    {counts['inbox']}")
    print(f"  Archived:         {counts['archive']}")
    print(f"  Check to Delete:  {counts['delete']}")
    if counts["errors"]:
        print(f"  Errors:           {counts['errors']}")
    if inbox_subjects:
        print(f"\nEmails left in inbox:")
        print("\n".join(inbox_subjects))


if __name__ == "__main__":
    import sys
    main(dry_run="--dry-run" in sys.argv)
