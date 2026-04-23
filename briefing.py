#!/usr/bin/env python3
"""
morning-briefing/briefing.py — Good Morning Astrid
Three sections: today's calendar · admin block todos · news (geopolitics, economy, AI)
"""

import json
import os
import re
import smtplib
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import anthropic
import urllib.request

SCRIPT_DIR = Path(__file__).parent

# ── CONFIG ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    env_path = SCRIPT_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    return {
        "anthropic_api_key": os.environ["ANTHROPIC_API_KEY"],
        "supabase_url":      os.environ.get("SUPABASE_URL", ""),
        "supabase_key":      os.environ.get("SUPABASE_KEY", ""),
        "ms_client_id":      os.environ.get("MS_CLIENT_ID", ""),
        "ms_client_secret":  os.environ.get("MS_CLIENT_SECRET", ""),
        "smtp": {
            "host":     "smtp.gmail.com",
            "user":     os.environ["SMTP_USER"],
            "password": os.environ["SMTP_PASSWORD"],
            "from":     os.environ["SMTP_USER"],
            "to":       os.environ["SMTP_TO"],
        },
    }

# ── NEWS ──────────────────────────────────────────────────────────────────────

RSS_FEEDS = {
    "geopolitics": [
        ("BBC World",      "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("Guardian World", "https://www.theguardian.com/world/rss"),
    ],
    "economy": [
        ("BBC Business",      "https://feeds.bbci.co.uk/news/business/rss.xml"),
        ("Guardian Business", "https://www.theguardian.com/business/rss"),
    ],
    "ai": [
        ("MIT Tech Review", "https://www.technologyreview.com/feed/"),
        ("The Verge AI",    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
        ("TechCrunch AI",   "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ],
}
MAX_ITEMS_PER_FEED = 6

def fetch_feed(name: str, url: str) -> list[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MorningBriefing/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)
        results = []
        for item in items[:MAX_ITEMS_PER_FEED]:
            title = (item.findtext("title") or "").strip()
            desc  = re.sub(r"<[^>]+>", "", (
                item.findtext("description") or
                item.findtext("atom:summary", namespaces=ns) or ""
            )).strip()
            if title:
                results.append({"source": name, "title": title, "desc": desc[:200]})
        print(f"  ✓ {name}: {len(results)} items")
        return results
    except Exception as e:
        print(f"  ✗ {name}: {e}")
        return []

def fetch_all_news() -> dict[str, list[dict]]:
    print("Fetching news...")
    result = {}
    for category, feeds in RSS_FEEDS.items():
        items = []
        for name, url in feeds:
            items.extend(fetch_feed(name, url))
        result[category] = items
    return result

# ── MICROSOFT CALENDAR ────────────────────────────────────────────────────────

TOKEN_PATH = SCRIPT_DIR / "ms_token.json"
TOKEN_URL  = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"

def refresh_ms_token(token: dict, cfg: dict) -> dict:
    data = urllib.parse.urlencode({
        "client_id":     cfg["ms_client_id"],
        "client_secret": cfg["ms_client_secret"],
        "refresh_token": token["refresh_token"],
        "grant_type":    "refresh_token",
        "scope":         "Calendars.Read User.Read offline_access",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        new_token = json.loads(resp.read())
    TOKEN_PATH.write_text(json.dumps(new_token, indent=2))
    return new_token

def fetch_todays_events(cfg: dict) -> list[dict]:
    print("Fetching today's calendar...")
    if not TOKEN_PATH.exists():
        print("  ⚠ No ms_token.json — skipping calendar")
        return []
    token = json.loads(TOKEN_PATH.read_text())
    try:
        token = refresh_ms_token(token, cfg)
    except Exception as e:
        print(f"  ⚠ Token refresh failed: {e}")
        return []

    import zoneinfo
    tz    = zoneinfo.ZoneInfo("Europe/Vienna")
    today = date.today()
    start = today
    end   = today + timedelta(days=1)

    url = (
        "https://graph.microsoft.com/v1.0/me/calendarView"
        f"?startDateTime={start.isoformat()}T00:00:00Z&endDateTime={end.isoformat()}T00:00:00Z"
        "&$select=subject,start,end,location,isAllDay,showAs"
        "&$orderby=start/dateTime&$top=50"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token['access_token']}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            events = json.loads(resp.read()).get("value", [])
        print(f"  ✓ {len(events)} events today")
        result = []
        for e in events:
            if e.get("isAllDay"):
                time_str = "all day"
                start_dt = None
                end_dt   = None
            else:
                s  = datetime.fromisoformat(e["start"]["dateTime"].replace("Z", "+00:00")).astimezone(tz)
                en = datetime.fromisoformat(e["end"]["dateTime"].replace("Z", "+00:00")).astimezone(tz)
                time_str = f"{s.strftime('%H:%M')}–{en.strftime('%H:%M')}"
                start_dt = s
                end_dt   = en
            result.append({
                "time":     time_str,
                "subject":  e["subject"],
                "location": e.get("location", {}).get("displayName", ""),
                "show_as":  e.get("showAs", "busy"),
                "start_dt": start_dt,
                "end_dt":   end_dt,
                "all_day":  e.get("isAllDay", False),
            })
        return result
    except Exception as e:
        print(f"  ✗ calendar: {e}")
        return []

def format_calendar(events: list[dict]) -> str:
    if not events:
        return "No events today."
    lines = []
    for e in events:
        show_as = e.get("show_as", "busy")
        if show_as == "free":
            continue  # skip reminders that don't require presence
        line = f"- {e['time']}: {e['subject']}"
        if show_as == "tentative": line += " [TENTATIVE]"
        if e["location"]:          line += f" @ {e['location']}"
        lines.append(line)
    return "\n".join(lines) if lines else "No commitments today."

# ── TODOS ─────────────────────────────────────────────────────────────────────

def fetch_todos(url: str, key: str) -> list[dict]:
    if not url or not key:
        return []
    print("Fetching todos...")
    req = urllib.request.Request(
        f"{url}/rest/v1/todos?status=eq.pending"
        "&order=priority.desc.nullslast,due_date.asc.nullslast"
        "&select=text,priority,due_date,notes,category",
        headers={"apikey": key, "Authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            todos = json.loads(resp.read())
        print(f"  ✓ {len(todos)} todos")
        return todos
    except Exception as e:
        print(f"  ✗ todos: {e}")
        return []


def fetch_astrid_blurb(url: str, key: str) -> str:
    if not url or not key:
        return ""
    req = urllib.request.Request(
        f"{url}/rest/v1/person_blurbs?person_name=eq.Astrid&select=blurb,updated_at&limit=1",
        headers={"apikey": key, "Authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            rows = json.loads(resp.read())
        if rows:
            print(f"  ✓ blurb fetched")
            return rows[0]["blurb"]
    except Exception as e:
        print(f"  ✗ blurb: {e}")
    return ""

def format_todos(todos: list[dict]) -> str:
    if not todos:
        return "No pending todos."
    lines = []
    for t in todos:
        line = f"- {t['text']}"
        if t.get("priority"): line += f" [p{t['priority']}]"
        if t.get("due_date"): line += f" [due: {t['due_date']}]"
        if t.get("category"): line += f" [{t['category']}]"
        if t.get("notes"):    line += f" — {t['notes']}"
        lines.append(line)
    return "\n".join(lines)

# ── CLAUDE ────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = "You are a sharp, direct briefing writer. Write tight, clear English. No filler. Lead with what matters."

def generate_briefing(events: list[dict], todos_text: str, news: dict[str, list[dict]], api_key: str) -> dict:
    client    = anthropic.Anthropic(api_key=api_key)
    today_str = date.today().strftime("%A, %d %B %Y")

    calendar_text = format_calendar(events)

    def fmt_news(items):
        return "\n".join(
            f"[{it['source']}] {it['title']}" + (f" — {it['desc']}" if it['desc'] else "")
            for it in items
        )

    prompt = f"""Today is {today_str}.

TODAY'S CALENDAR:
{calendar_text}

PENDING TODOS:
{todos_text}

GEOPOLITICS HEADLINES:
{fmt_news(news.get('geopolitics', []))}

ECONOMY HEADLINES:
{fmt_news(news.get('economy', []))}

AI HEADLINES:
{fmt_news(news.get('ai', []))}

Produce exactly three sections with these markers:

---CALENDAR---
2–4 sentence overview of today. What kind of day is it — packed, light, fragmented? Highlight anything that needs attention or preparation. If there are free blocks (gaps between meetings, or open afternoon/morning), identify them explicitly as admin time.

---TODOS---
Look at the free blocks you identified in the calendar. Pick 3–5 todos from the list that are best suited to those blocks — consider priority, due date, and rough time needed. For each, say which block it fits and why it's the right call today. Be direct and specific, not generic.

---NEWS---
**GEOPOLITICS** (2–3 bullets — one crisp sentence each, source in brackets)
**ECONOMY** (2–3 bullets)
**AI** (2–3 bullets)"""

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()

    def extract(start, end, text):
        if start not in text:
            return ""
        part = text.split(start)[1]
        if end and end in part:
            part = part.split(end)[0]
        return part.strip()

    return {
        "calendar": extract("---CALENDAR---", "---TODOS---",   raw),
        "todos":    extract("---TODOS---",    "---NEWS---",    raw),
        "news":     extract("---NEWS---",     None,            raw),
    }

# ── EMAIL ─────────────────────────────────────────────────────────────────────

def markdown_to_html(text: str) -> str:
    lines = text.split("\n")
    html_lines = []
    in_list = False
    for line in lines:
        line = line.strip()
        if not line:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            continue
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        if line.startswith("- ") or line.startswith("• "):
            if not in_list:
                html_lines.append('<ul style="margin:6px 0;padding-left:20px">')
                in_list = True
            html_lines.append(f'<li style="margin-bottom:6px">{line[2:]}</li>')
        else:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            if line.startswith("<strong>"):
                html_lines.append(f'<p style="margin:14px 0 4px;color:#c8a96e;font-size:13px;text-transform:uppercase;letter-spacing:1px">{line}</p>')
            else:
                html_lines.append(f'<p style="margin:4px 0">{line}</p>')
    if in_list:
        html_lines.append("</ul>")
    return "\n".join(html_lines)

def build_html_email(calendar_md: str, todos_md: str, news_md: str, blurb: str = "") -> str:
    today_str = date.today().strftime("%A, %d %B %Y")

    def card(icon, title, body_html):
        if not body_html.strip():
            return ""
        return f"""
  <div style="background:#161b22;border:1px solid rgba(255,255,255,0.08);border-radius:12px;padding:16px 20px;margin-bottom:16px">
    <div style="font-size:10px;text-transform:uppercase;letter-spacing:1.5px;color:#7d8590;font-weight:600;margin-bottom:12px">{icon} {title}</div>
    <div style="font-size:14px;line-height:1.6;color:#e6edf3">{body_html}</div>
  </div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0d1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#e6edf3">
<div style="max-width:600px;margin:0 auto;padding:24px 16px">
  <div style="border-bottom:1px solid rgba(255,255,255,0.1);padding-bottom:16px;margin-bottom:20px">
    <h1 style="margin:0;font-size:22px;font-weight:700">Good Morning Astrid</h1>
    <p style="margin:4px 0 0;font-size:13px;color:#7d8590">{today_str}</p>
  </div>
  {card("📅", "Today", markdown_to_html(calendar_md))}
  {card("✅", "Admin Blocks", markdown_to_html(todos_md))}
  {card("💬", "Your Todos", f'<p style="margin:0 0 10px;font-style:italic;color:#adbac7">{blurb}</p>' if blurb else "")}
  {card("📰", "News", markdown_to_html(news_md))}
  <p style="font-size:11px;color:#7d8590;text-align:center;margin-top:20px">
    Generated {datetime.now().strftime("%H:%M")} · morning-briefing
  </p>
</div></body></html>"""

def send_email(html: str, cfg: dict):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Good Morning Astrid — {date.today().strftime('%a %d %b')}"
    msg["From"]    = cfg["smtp"]["from"]
    msg["To"]      = cfg["smtp"]["to"]
    msg.attach(MIMEText(html, "html", "utf-8"))
    print(f"Sending to {cfg['smtp']['to']}...")
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
        server.ehlo()
        server.starttls()
        server.login(cfg["smtp"]["user"], cfg["smtp"]["password"])
        server.sendmail(cfg["smtp"]["from"], cfg["smtp"]["to"], msg.as_string())
    print("  ✓ Email sent")

# ── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print(f"\nGood Morning Astrid — {date.today().isoformat()}\n")
    cfg = load_config()

    news   = fetch_all_news()
    todos  = fetch_todos(cfg["supabase_url"], cfg["supabase_key"])
    blurb  = fetch_astrid_blurb(cfg["supabase_url"], cfg["supabase_key"])
    events = fetch_todays_events(cfg)

    todos_text = format_todos(todos)

    print("\nGenerating briefing...")
    sections = generate_briefing(events, todos_text, news, cfg["anthropic_api_key"])

    print("\n--- CALENDAR ---\n", sections["calendar"])
    print("\n--- TODOS ---\n", sections["todos"])
    print("\n--- NEWS (truncated) ---\n", sections["news"][:300])

    html = build_html_email(sections["calendar"], sections["todos"], sections["news"], blurb)
    send_email(html, cfg)
    print("\nDone.")

if __name__ == "__main__":
    main()
