#!/usr/bin/env python3
"""
coaching.py — Weekly and mid-week coaching emails from Supabase tracker data.
No Garmin dependency. Two modes:
  weekly  (Monday 1am)  — full review of the past week + 3 prior weeks for context
  midweek (Thursday 1am) — check-in on Mon–Wed progress + Thu–Sun plan

VPS cron:
  0 1 * * 1  cd /root/morning-briefing && python3 coaching.py weekly
  0 1 * * 4  cd /root/morning-briefing && python3 coaching.py midweek

Env vars (from .env shared with briefing.py):
  ANTHROPIC_API_KEY (required)
  SMTP_USER, SMTP_PASSWORD, SMTP_TO (required)
  COACHING_TO  (optional override for recipient)
  COACHING_SB_URL, COACHING_SB_KEY  (optional — fall back to hardcoded goal-tracker values)
"""

import json
import os
import re
import smtplib
import sys
import urllib.request
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

try:
    import anthropic
except ImportError:
    print("ERROR: pip install anthropic")
    sys.exit(1)

SCRIPT_DIR = Path(__file__).parent

# ── CONFIG ────────────────────────────────────────────────────────────────────

def load_env():
    env_path = SCRIPT_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

# ── SUPABASE (goal-tracker / astrid-efficiency project) ───────────────────────

# Public anon key — read-only, safe to hardcode (same as in garmin_sync.py)
_DEFAULT_SB_URL = "https://ykbabwkyojlwculjmbrw.supabase.co"
_DEFAULT_SB_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InlrYmFid2t5b2psd2N1bGptYnJ3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzY1MTg0NzMsImV4cCI6MjA5MjA5NDQ3M30."
    "4lYnVcgv1o9OvXGMtrbjwKhXZ8Yybqln08aci_ZM9mI"
)

SB_URL = SB_KEY = None  # set in main() after load_env()


def sb_get(table: str, params: str = "") -> list:
    url = f"{SB_URL}/rest/v1/{table}?{params}"
    req = urllib.request.Request(url, headers={
        "apikey": SB_KEY,
        "Authorization": f"Bearer {SB_KEY}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  ✗ Supabase {table}: {e}")
        return []


def fetch_week_data(week_start: str, week_end: str) -> dict:
    habits   = sb_get("daily_habits",  f"date=gte.{week_start}&date=lte.{week_end}&select=*")
    cardgym  = sb_get("cardio_gym",    f"date=gte.{week_start}&date=lte.{week_end}&select=*")
    sessions = sb_get("golf_sessions", f"date=gte.{week_start}&date=lte.{week_end}&select=*")
    return dict(habits=habits, cardgym=cardgym, sessions=sessions)


def fetch_rounds(since: str, n: int = 10) -> list:
    return sb_get("golf_rounds", f"date=gte.{since}&order=date.desc&limit={n}&select=*")


# ── DATE HELPERS ──────────────────────────────────────────────────────────────

def week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


# ── SUMMARISE A WEEK ─────────────────────────────────────────────────────────

CARDIO_NAMES = ["Zone 2", "Long Run", "Tempo", "VO2 Max"]
GYM_NAMES    = ["Weights #1", "Weights #2", "Mobility"]


def summarise_week(data: dict, week_start: str, week_end: str, days_available: int = 7) -> dict:
    mirror = protein = stretch = hydration = alcohol = 0
    for h in data["habits"]:
        if h.get("mirror"):  mirror    += 1
        if h.get("protein"): protein   += 1
        if h.get("stretch"): stretch   += 1
        if (h.get("water_glasses") or 0) >= 6: hydration += 1
        if h.get("alcohol"): alcohol   += 1

    cardio_done = {}
    gym_done    = {}
    for row in data["cardgym"]:
        idx = row["slot_index"]
        val = row.get("value") or 0
        if row["type"] == "cardio":
            cardio_done[idx] = cardio_done.get(idx, 0) + val
        else:
            gym_done[idx] = gym_done.get(idx, 0) + val

    cardio          = {name: cardio_done.get(i, 0) for i, name in enumerate(CARDIO_NAMES)}
    weights_done    = sum(1 for i in [0, 1] if gym_done.get(i, 0) >= 1)
    mobility_done   = gym_done.get(2, 0) >= 1

    sess_counts = {}
    for s in data["sessions"]:
        sess_counts[s["cat"]] = sess_counts.get(s["cat"], 0) + 1

    has_data = bool(data["habits"] or data["cardgym"] or data["sessions"])

    return {
        "week_start":    week_start,
        "week_end":      week_end,
        "days_available": days_available,
        "has_data":      has_data,
        "mirror":        mirror,
        "protein":       protein,
        "stretch":       stretch,
        "hydration":     hydration,
        "alcohol":       alcohol,
        "cardio":        cardio,
        "weights":       weights_done,
        "mobility":      mobility_done,
        "golf_sessions": sess_counts,
    }


def format_week_detail(s: dict, label: str) -> str:
    if not s["has_data"]:
        return f"{label} ({s['week_start']} — {s['week_end']}): no tracking data"

    d = s["days_available"]
    lines = [f"{label} ({s['week_start']} — {s['week_end']}, {d} days tracked):"]
    lines.append(f"  Habits: mirror {s['mirror']}/{d}, protein {s['protein']}/{d}, "
                 f"stretch {s['stretch']}/{d}, hydration {s['hydration']}/{d}, alcohol {s['alcohol']} days")

    cardio_parts = []
    for name in CARDIO_NAMES:
        v = s["cardio"].get(name, 0)
        if v >= 2:   cardio_parts.append(f"{name}: ✓×2")
        elif v == 1: cardio_parts.append(f"{name}: ✓")
        else:        cardio_parts.append(f"{name}: –")
    lines.append(f"  Cardio: {', '.join(cardio_parts)}")
    lines.append(f"  Gym: weights {s['weights']}/2, mobility {'✓' if s['mobility'] else '–'}")

    golf = s["golf_sessions"]
    lines.append(f"  Golf: rounds={golf.get('round', 0)}, range={golf.get('range', 0)}, "
                 f"sga={golf.get('sga', 0)}, putt/chip={golf.get('putt', 0)}")
    return "\n".join(lines)


def format_week_compact(s: dict, label: str) -> str:
    if not s["has_data"]:
        return f"{label} ({s['week_start']}): no data"
    c = s["cardio"]
    z2 = "✓" if c.get("Zone 2", 0) >= 1 else "–"
    lr = "✓" if c.get("Long Run", 0) >= 1 else "–"
    tm = "✓" if c.get("Tempo", 0) >= 1 else "–"
    v2 = "✓" if c.get("VO2 Max", 0) >= 1 else "–"
    g  = s["golf_sessions"]
    return (f"{label} ({s['week_start']}): "
            f"Z2={z2} LR={lr} Tmp={tm} VO2={v2} | wts {s['weights']}/2 | "
            f"golf rounds={g.get('round',0)} range={g.get('range',0)} "
            f"sga={g.get('sga',0)} putt={g.get('putt',0)} | "
            f"mirror {s['mirror']}/{s['days_available']}, protein {s['protein']}/{s['days_available']}")


def format_rounds(rounds: list) -> str:
    if not rounds:
        return "ROUNDS: none in this period"
    lines = ["ROUNDS (most recent first):"]
    for r in rounds:
        hd     = r.get("holes_data") or []
        played = [h for h in hd if h.get("par") not in (None, "") and h.get("score") not in (None, "")]
        if played:
            delta  = sum(int(h["score"]) - int(h["par"]) for h in played)
            gir    = sum(1 for h in played if h.get("gir"))
            p3     = sum(1 for h in played if h.get("p3"))
            fw_el  = [h for h in played if int(h.get("par", 4)) != 3]
            fw     = sum(1 for h in fw_el if h.get("fw"))
            db     = sum(1 for h in played if int(h["score"]) >= int(h["par"]) + 2)
            lines.append(
                f"  {r['date']} {r.get('course', '')} {'[Comp]' if r.get('comp') else ''}: "
                f"{len(played)}h {'+' if delta > 0 else ''}{delta} | "
                f"GIR:{gir}/{len(played)} FW:{fw}/{len(fw_el)} 3P:{p3} Dbl:{db}"
            )
        else:
            lines.append(f"  {r['date']} {r.get('course', '')} — no hole data")
    return "\n".join(lines)


# ── GOALS CONTEXT ─────────────────────────────────────────────────────────────

GOALS_CONTEXT = """ATHLETE PROFILE:
- COO, 3 teenage children, husband travels Mon–Thu. Real time constraints — acknowledge but don't use as excuses.
- GOLF: Handicap 10→9 by Nov 2026. Upcoming: NÖ Meisterschaften May 14-17, Staatsmeisterschaften May 22-25.
  Weekly targets: ≥1 drill per category (Range/Score, Range/Drive, SGA, Putt+Chip), mirror putting 5×/week, ≥1 game per category.
- RUNNING: 5k PB (24:30) and VO2 Max 45→48 by Nov 2026. Back to full training from April 13 2026 (injury recovery).
  Target: 3 runs/week — Zone 2 (~7.5km), 1 long run, 1 tempo/interval. Total ~20-25km/week.
  Max HR: 193. Zone 2 = ~126-145 bpm. Tempo = ~155-170. VO2 Max = 175+ bpm.
- STRENGTH: 2× weights/week with PT Martin + protein ≥90g/day.
- HABITS: Hydration 6 glasses/day, alcohol ≤3×/week, stretch/foam roll 5×/week, mobility 1×/week.

DATA NOTE: Weights are almost never logged on Garmin (watch gets in the way). The tracker is the ONLY reliable source for weights. Trust the tracker.
TRACKING NOTE: Tracking only started April 20 2026 — do not evaluate or compare weeks before that date."""

COACHING_SYSTEM = (
    "You are a direct, no-nonsense personal coach with full visibility of the athlete's tracker data. "
    "Your feedback is honest and specific — no generic encouragement. If something is behind, say so clearly. "
    "If something is good, acknowledge it briefly. The athlete explicitly asked to be pushed. "
    "Formatting: plain text only, dashes for bullet lists, numbered section headers as written in the prompt. "
    "No markdown bold, no asterisks."
)


# ── CLAUDE ────────────────────────────────────────────────────────────────────

def call_claude(prompt: str, max_tokens: int = 900) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return "(ANTHROPIC_API_KEY not set — cannot generate coaching message)"
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        system=COACHING_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


# ── WEEKLY REVIEW (Monday 1am) ────────────────────────────────────────────────

def build_weekly_review(today: date) -> tuple[str, str]:
    # At 1am Monday, the past week is last Mon–Sun
    past_sun = today - timedelta(days=1)        # Sunday
    past_mon = past_sun - timedelta(days=6)     # previous Monday

    print(f"Weekly review: {past_mon.isoformat()} — {past_sun.isoformat()}")

    # Fetch reviewed week + 3 prior weeks (4 total)
    summaries = []
    for i in range(4):
        ws = past_mon - timedelta(weeks=i)
        we = ws + timedelta(days=6)
        print(f"  Fetching week {i+1}: {ws.isoformat()} — {we.isoformat()}")
        data = fetch_week_data(ws.isoformat(), we.isoformat())
        days = min(7, (min(we, today - timedelta(days=1)) - ws).days + 1)
        summaries.append(summarise_week(data, ws.isoformat(), we.isoformat(), days_available=days))

    # Rounds from the past 8 weeks for context
    rounds_since = (past_mon - timedelta(weeks=7)).isoformat()
    rounds = fetch_rounds(since=rounds_since, n=12)

    reviewed_text = format_week_detail(summaries[0], "REVIEWED WEEK")
    prior_text = "\n".join(
        format_week_compact(s, f"  Prior week -{i}")
        for i, s in enumerate(summaries[1:], 1)
    )
    rounds_text = format_rounds(rounds)

    prompt = f"""{GOALS_CONTEXT}

{reviewed_text}

PRIOR WEEKS (compact — for trend context only):
{prior_text}

{rounds_text}

Write a WEEKLY COACHING REVIEW for the week ending {past_sun.isoformat()}.

Use this exact structure:
1. WEEK SUMMARY
2. GOLF
3. FITNESS
4. HABITS
5. NEXT WEEK FOCUS

Guidelines:
- Week Summary: 2-3 sentences, honest overall verdict.
- Golf: what went well, what didn't, specific practice priorities for next week; analyse rounds if played.
- Fitness: running sessions vs 3/week target, weights vs 2/week — honest assessment; if targets missed say so.
- Habits: only flag what actually matters (off track or notably good); skip anything on target.
- Next Week Focus: 3 specific, prioritised actions. Be direct, actionable, ordered by importance.
Do not start with "Astrid," — go straight to section 1."""

    body    = call_claude(prompt, max_tokens=900)
    subject = f"Weekly Coaching Review — w/e {past_sun.isoformat()}"
    return subject, body


# ── MID-WEEK CHECK-IN (Thursday 1am) ─────────────────────────────────────────

def build_midweek_checkin(today: date) -> tuple[str, str]:
    # At 1am Thursday: Mon–Wed = 3 days done, Thu–Sun = 4 days left
    this_mon = week_monday(today)
    this_wed = today - timedelta(days=1)   # Wednesday

    last_mon = this_mon - timedelta(weeks=1)
    last_sun = this_mon - timedelta(days=1)

    print(f"Midweek check-in: {this_mon.isoformat()} — {this_wed.isoformat()} (3 days)")
    print(f"Previous week:    {last_mon.isoformat()} — {last_sun.isoformat()}")

    current_data  = fetch_week_data(this_mon.isoformat(), this_wed.isoformat())
    previous_data = fetch_week_data(last_mon.isoformat(), last_sun.isoformat())
    rounds        = fetch_rounds(since=(last_mon - timedelta(weeks=3)).isoformat(), n=8)

    current_s  = summarise_week(current_data,  this_mon.isoformat(), this_wed.isoformat(), days_available=3)
    previous_s = summarise_week(previous_data, last_mon.isoformat(), last_sun.isoformat(), days_available=7)

    current_text  = format_week_detail(current_s,  "THIS WEEK so far (Mon–Wed)")
    previous_text = format_week_compact(previous_s, "LAST WEEK (complete)")
    rounds_text   = format_rounds(rounds)

    prompt = f"""{GOALS_CONTEXT}

{current_text}

{previous_text}

{rounds_text}

It is Thursday morning — 3 days done (Mon–Wed), 4 days remaining (Thu–Sun).

Write a MID-WEEK CHECK-IN. Use this exact structure:
1. CHECK-IN
2. GAPS
3. FINISH STRONG

Guidelines:
- Check-In: 2 sentences — honest verdict on the first half of the week.
- Gaps: which weekly targets are at risk of being missed by Sunday? Be specific and direct. List only real gaps, not everything.
- Finish Strong: 3 specific, actionable priorities for Thu–Sun. Tell her exactly what to do.
Total max 250 words. Tight and direct — this is a quick check-in, not a full review.
Do not start with "Astrid," — go straight to section 1."""

    body    = call_claude(prompt, max_tokens=500)
    subject = f"Mid-Week Check-In — {this_wed.isoformat()}"
    return subject, body


# ── EMAIL ─────────────────────────────────────────────────────────────────────

def body_to_html(text: str) -> str:
    lines   = text.split("\n")
    html    = []
    in_list = False

    for line in lines:
        s = line.strip()
        if not s:
            if in_list:
                html.append("</ul>")
                in_list = False
            continue

        # Numbered section headers: "1. WEEK SUMMARY" etc.
        if re.match(r"^\d+\. [A-Z /–-]+$", s):
            if in_list:
                html.append("</ul>")
                in_list = False
            html.append(
                f'<p style="margin:20px 0 6px;color:#c8a96e;font-size:11px;'
                f'text-transform:uppercase;letter-spacing:1.2px;font-weight:700">{s}</p>'
            )
        elif s.startswith("- "):
            if not in_list:
                html.append('<ul style="margin:4px 0 8px;padding-left:20px">')
                in_list = True
            html.append(f'<li style="margin-bottom:5px">{s[2:]}</li>')
        else:
            if in_list:
                html.append("</ul>")
                in_list = False
            html.append(f'<p style="margin:4px 0 6px">{s}</p>')

    if in_list:
        html.append("</ul>")
    return "\n".join(html)


def build_html_email(subject: str, body: str) -> str:
    body_html = body_to_html(body)
    generated = datetime.now().strftime("%H:%M")
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0d1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#e6edf3">
<div style="max-width:600px;margin:0 auto;padding:24px 16px">
  <div style="border-bottom:1px solid rgba(255,255,255,0.1);padding-bottom:16px;margin-bottom:20px">
    <h1 style="margin:0;font-size:20px;font-weight:700">{subject}</h1>
  </div>
  <div style="background:#161b22;border:1px solid rgba(255,255,255,0.08);border-radius:12px;
              padding:20px 24px;font-size:14px;line-height:1.7;color:#e6edf3">
    {body_html}
  </div>
  <p style="font-size:11px;color:#7d8590;text-align:center;margin-top:20px">
    Generated {generated} · coaching.py
  </p>
</div>
</body></html>"""


def send_email(subject: str, html_body: str):
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASSWORD", "")
    smtp_to   = os.environ.get("COACHING_TO") or os.environ.get("SMTP_TO", "")

    if not smtp_pass:
        print("  ✗ SMTP_PASSWORD not set — printing plain body instead:")
        print(html_body)
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = smtp_user
    msg["To"]      = smtp_to
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
        server.ehlo()
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, smtp_to, msg.as_string())
    print(f"  ✓ Email sent to {smtp_to}")


# ── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    load_env()

    global SB_URL, SB_KEY
    SB_URL = os.environ.get("COACHING_SB_URL", _DEFAULT_SB_URL)
    SB_KEY = os.environ.get("COACHING_SB_KEY", _DEFAULT_SB_KEY)

    today = date.today()

    if len(sys.argv) > 1:
        mode = sys.argv[1].lower()
    elif today.weekday() == 0:
        mode = "weekly"
        print(f"No mode arg — detected 'weekly' from {today.strftime('%A')}")
    elif today.weekday() == 3:
        mode = "midweek"
        print(f"No mode arg — detected 'midweek' from {today.strftime('%A')}")
    else:
        print(f"ERROR: no mode arg and today is {today.strftime('%A')} (not Mon/Thu). "
              f"Pass 'weekly' or 'midweek' explicitly.")
        sys.exit(1)

    if mode not in ("weekly", "midweek"):
        print(f"ERROR: unknown mode '{mode}'. Use 'weekly' or 'midweek'.")
        sys.exit(1)

    print(f"\nCoaching — {today.isoformat()} — mode: {mode}\n")

    if mode == "weekly":
        subject, body = build_weekly_review(today)
    else:
        subject, body = build_midweek_checkin(today)

    print(f"\n--- {subject} ---\n{body}\n--- END ---\n")
    send_email(subject, build_html_email(subject, body))
    print("Done.")


if __name__ == "__main__":
    main()
