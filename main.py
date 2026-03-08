import os
import json
import requests
import anthropic
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, jsonify
from twilio.rest import Client
import pytz

app = Flask(__name__)

# ── CONFIG ──
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_KEY")
TWILIO_SID      = os.environ.get("TWILIO_SID")
TWILIO_TOKEN    = os.environ.get("TWILIO_TOKEN")
TWILIO_FROM     = os.environ.get("TWILIO_FROM")
REED_PHONE      = os.environ.get("REED_PHONE")
BRIEFING_HOUR   = int(os.environ.get("BRIEFING_HOUR", "8"))
TIMEZONE        = os.environ.get("TIMEZONE", "America/New_York")

# ── SIMPLE FILE-BASED DB ──
SEEN_JOBS_FILE = "seen_jobs.json"

def load_seen():
    try:
        with open(SEEN_JOBS_FILE) as f:
            return set(json.load(f))
    except:
        return set()

def save_seen(seen):
    with open(SEEN_JOBS_FILE, "w") as f:
        json.dump(list(seen), f)

def send_sms(body):
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(
            body=body,
            from_="whatsapp:" + TWILIO_FROM,
            to="whatsapp:" + REED_PHONE
            to=REED_PHONE
        )
        print(f"SMS sent: {body[:60]}...")
    except Exception as e:
        print(f"SMS error: {e}")

def ask_claude(prompt, use_search=False):
    headers = {
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}]
    }
    if use_search:
        headers["anthropic-beta"] = "web-search-2025-03-05"
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]

    r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
    data = r.json()
    if data.get("error"):
        raise Exception(data["error"]["message"])

    # Handle tool use follow-up
    if data.get("stop_reason") == "tool_use" and use_search:
        body2 = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
            "messages": [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": data["content"]}
            ]
        }
        r2 = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body2)
        data = r2.json()

    text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            text += block["text"]
    return text.strip()

# ── TASK 1: MORNING BRIEFING (8 AM EST daily) ──
def morning_briefing():
    print("Running morning briefing...")
    try:
        prompt = """Give Reed his morning briefing. Keep it under 300 characters total for SMS.

Reed is job hunting (wants 9-5 office work, no degree needed), saving for a car, learning AI.

Format exactly like this (use line breaks):
Good Morning Reed 🌅
[One sentence: weather vibe or motivation for today]
🎯 Focus: [One specific thing he should do today toward his goals]
💼 Job Tip: [One quick actionable job search tip]

Keep it punchy, real, no fluff."""

        text = ask_claude(prompt, use_search=True)
        send_sms(text)
        print("Morning briefing sent.")
    except Exception as e:
        print(f"Morning briefing error: {e}")

# ── TASK 2: JOB SCAN (every 6 hours) ──
def job_scan():
    print("Running job scan...")
    try:
        seen = load_seen()

        prompt = """Search for real, currently open US job listings for someone with no degree who wants a 9-5 office job where they can dress professionally. Look for: office coordinator, admin assistant, customer success rep, sales rep, front desk, scheduling coordinator, operations assistant, receptionist, data entry specialist.

Return ONLY a JSON array, no other text:
[{"title":"Job Title","company":"Company","location":"City ST","pay":"$X/hr or $Xk","id":"company-title-location-slug"}]

Return up to 5 jobs. Real listings only."""

        result = ask_claude(prompt, use_search=True)

        # Parse JSON
        import re
        match = re.search(r'\[.*\]', result, re.DOTALL)
        if not match:
            print("No jobs parsed.")
            return

        jobs = json.loads(match.group())
        new_jobs = [j for j in jobs if j.get("id") not in seen]

        if not new_jobs:
            print("No new jobs found.")
            return

        # Text each new job (max 3 per scan to avoid spam)
        for job in new_jobs[:3]:
            msg = f"🔍 New Job Found!\n{job['title']} @ {job['company']}\n📍 {job['location']}\n💰 {job.get('pay','Pay Not Listed')}\n\nReply STOP to pause alerts."
            send_sms(msg)
            seen.add(job.get("id"))

        save_seen(seen)
        print(f"Sent {min(len(new_jobs),3)} new job alerts.")
    except Exception as e:
        print(f"Job scan error: {e}")

# ── TASK 3: WEEKLY RECAP (Sunday 6 PM EST) ──
def weekly_recap():
    print("Running weekly recap...")
    try:
        prompt = """Write Reed a brief weekly job search motivation text. Keep under 280 characters.

Reed is: hunting for a 9-5 office job, saving for a car, learning AI/tech.

Format:
📊 Weekly Check-In
[2 sentences: acknowledge the grind, specific encouragement]
This Week: [One concrete action to take]"""

        text = ask_claude(prompt)
        send_sms(text)
        print("Weekly recap sent.")
    except Exception as e:
        print(f"Weekly recap error: {e}")

# ── SCHEDULER ──
scheduler = BackgroundScheduler(timezone=TIMEZONE)

# Morning briefing — 8 AM EST daily
scheduler.add_job(
    morning_briefing,
    CronTrigger(hour=BRIEFING_HOUR, minute=0, timezone=TIMEZONE),
    id="morning_briefing",
    replace_existing=True
)

# Job scan — every 6 hours
scheduler.add_job(
    job_scan,
    CronTrigger(hour="*/6", minute=30, timezone=TIMEZONE),
    id="job_scan",
    replace_existing=True
)

# Weekly recap — Sunday 6 PM EST
scheduler.add_job(
    weekly_recap,
    CronTrigger(day_of_week="sun", hour=18, minute=0, timezone=TIMEZONE),
    id="weekly_recap",
    replace_existing=True
)

scheduler.start()
print(f"Scheduler started. Morning briefing at {BRIEFING_HOUR}:00 {TIMEZONE}")

# ── ROUTES ──
@app.route("/")
def index():
    return jsonify({"status": "Reed AI Backend Running", "time": datetime.now().isoformat()})

@app.route("/ping")
def ping():
    return jsonify({"ok": True})

@app.route("/test-sms")
def test_sms():
    send_sms("Reed AI Is Online And Running. Your Morning Briefings And Job Alerts Are Active. 🤖")
    return jsonify({"sent": True})

@app.route("/run-briefing")
def run_briefing():
    morning_briefing()
    return jsonify({"ran": "morning_briefing"})

@app.route("/run-jobs")
def run_jobs():
    job_scan()
    return jsonify({"ran": "job_scan"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
