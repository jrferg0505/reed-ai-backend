import os
import json
import requests
import anthropic
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import STATE_RUNNING
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, jsonify, request
from flask_cors import CORS
from twilio.rest import Client
import pytz
import threading

app = Flask(__name__)
CORS(app)

ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_KEY")
TWILIO_SID      = os.environ.get("TWILIO_SID")
TWILIO_TOKEN    = os.environ.get("TWILIO_TOKEN")
TWILIO_FROM     = os.environ.get("TWILIO_FROM")
REED_PHONE      = os.environ.get("REED_PHONE")
BRIEFING_HOUR   = int(os.environ.get("BRIEFING_HOUR", "8"))
TIMEZONE        = os.environ.get("TIMEZONE", "America/New_York")

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

def send_whatsapp(body):
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(
            body=body,
            from_="whatsapp:" + TWILIO_FROM,
            to="whatsapp:" + REED_PHONE
        )
        print(f"WhatsApp sent: {body[:60]}...")
    except Exception as e:
        print(f"WhatsApp error: {e}")

def ask_claude(prompt, use_search=False, max_tokens=1024):
    headers = {
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}]
    }
    if use_search:
        headers["anthropic-beta"] = "web-search-2025-03-05"
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]

    r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
    data = r.json()
    if data.get("error"):
        raise Exception(data["error"]["message"])

    if data.get("stop_reason") == "tool_use" and use_search:
        body2 = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": max_tokens,
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
    # Track spend
    try:
        usage = data.get("usage", {})
        track_spend(usage.get("input_tokens", 0), usage.get("output_tokens", 0))
    except:
        pass
    return text.strip()

def morning_briefing():
    print("Running morning briefing...")
    try:
        prompt = """Give Reed his morning briefing. Search the web for Indianapolis weather today.
Format exactly like this (use line breaks, keep total under 400 characters):
Good Morning Reed 🌅
☀️ Weather: [Today's Indianapolis weather — temp + conditions in one line]
💬 [One punchy motivational quote — real, not cheesy]
🎯 Focus: [One specific action toward his goals today]
💼 Job Tip: [One quick actionable job search tip for today]

Reed: Indianapolis, hunting for $18+/hr office job, saving for car, learning AI. Be direct, capitalize every word."""
        text = ask_claude(prompt, use_search=True)
        send_whatsapp(text)
        print("Morning briefing sent.")
    except Exception as e:
        print(f"Morning briefing error: {e}")

def job_scan():
    print("Running job scan...")
    try:
        seen = load_seen()
        prompt = """Search Indeed, LinkedIn, ZipRecruiter, and Google Jobs for real currently open office jobs in Indianapolis Indiana. Requirements: no college degree required, pay $18/hour or more (or $37,000+ salary), in-person office work (no remote), Monday-Friday schedule. Look for roles like: office coordinator, admin assistant, customer success rep, inside sales rep, front desk coordinator, scheduling coordinator, operations assistant, receptionist, account manager, office manager, data entry specialist, or any similar professional office role.

Return ONLY a JSON array, no other text:
[{"title":"Job Title","company":"Company","location":"City, IN","pay":"$X/hr or $Xk/yr","id":"company-title-city-slug"}]

Return up to 5 real current listings only. Skip anything under $18/hr or that requires a degree."""
        result = ask_claude(prompt, use_search=True)
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
        for job in new_jobs[:3]:
            msg = f"🔍 New Job Found!\n{job['title']} @ {job['company']}\n📍 {job['location']}\n💰 {job.get('pay','Pay Not Listed')}\n\nOpen Reed AI To Save It."
            send_whatsapp(msg)
            seen.add(job.get("id"))
        save_seen(seen)
        print(f"Sent {min(len(new_jobs),3)} new job alerts.")
    except Exception as e:
        print(f"Job scan error: {e}")

def weekly_recap():
    print("Running weekly recap...")
    try:
        prompt = """Write Reed a brief weekly job search motivation text. Keep under 280 characters.
Reed is: hunting for a 9-5 office job in Indianapolis, saving for a car, learning AI/tech.
Format:
📊 Weekly Check-In
[2 sentences: acknowledge the grind, specific encouragement]
This Week: [One concrete action to take]"""
        text = ask_claude(prompt)
        send_whatsapp(text)
        print("Weekly recap sent.")
    except Exception as e:
        print(f"Weekly recap error: {e}")

def run_agent_job_scan():
    try:
        send_whatsapp("🤖 Starting Job Scan Now. I'll Text You What I Find...")
        prompt = """Search Indeed, LinkedIn, ZipRecruiter, and Google Jobs for real currently open office jobs in Indianapolis Indiana. Requirements: no college degree required, pay $18/hour or more (or $37,000+ salary), in-person office work only (no remote), Monday-Friday schedule. Look for: office coordinator, admin assistant, customer success rep, inside sales rep, front desk coordinator, scheduling coordinator, operations assistant, receptionist, account manager, data entry specialist, or similar professional office roles.

Return ONLY a JSON array, no other text:
[{"title":"Job Title","company":"Company","location":"City, IN","pay":"$X/hr or $Xk/yr","applyUrl":"direct job listing URL or empty string","id":"company-title-city-slug"}]

Return up to 5 real current listings. Skip anything under $18/hr or that requires a degree."""
        result = ask_claude(prompt, use_search=True, max_tokens=2048)
        import re
        match = re.search(r'\[.*\]', result, re.DOTALL)
        if not match:
            send_whatsapp("❌ Couldn't Find Jobs Right Now. Try Again In A Few Minutes.")
            return
        jobs = json.loads(match.group())
        if not jobs:
            send_whatsapp("😕 No Jobs Found Right Now. I'll Keep Checking Every 6 Hours.")
            return
        send_whatsapp(f"✅ Found {len(jobs)} Jobs For You:")
        for job in jobs[:5]:
            msg = f"💼 {job['title']}\n🏢 {job['company']}\n📍 {job['location']}\n💰 {job.get('pay','Not Listed')}"
            if job.get('applyUrl'):
                msg += f"\n🔗 {job['applyUrl']}"
            send_whatsapp(msg)
    except Exception as e:
        send_whatsapp(f"❌ Job Scan Error: {str(e)[:100]}")
        print(f"Agent job scan error: {e}")

def run_agent_briefing():
    try:
        morning_briefing()
    except Exception as e:
        send_whatsapp(f"❌ Briefing Error: {str(e)[:100]}")

def run_agent_task(task_type, custom_prompt=None):
    try:
        if task_type == "job_scan":
            run_agent_job_scan()
        elif task_type == "briefing":
            run_agent_briefing()
        elif task_type == "custom" and custom_prompt:
            send_whatsapp("🤖 Working On It...")
            result = ask_claude(
                f"""You are Reed's personal AI assistant. Reed is in Indianapolis, works at a donut shop, job hunting for office work, saving for a car, learning AI. Be direct, no fluff, capitalize every word.
Task: {custom_prompt}
Keep response under 500 characters.""",
                use_search=True
            )
            send_whatsapp(f"✅ Done:\n{result}")
    except Exception as e:
        send_whatsapp(f"❌ Error: {str(e)[:100]}")

scheduler = BackgroundScheduler(timezone=TIMEZONE)
scheduler.add_job(morning_briefing, CronTrigger(hour=BRIEFING_HOUR, minute=0, timezone=TIMEZONE), id="morning_briefing", replace_existing=True)
scheduler.add_job(job_scan, CronTrigger(hour="*/6", minute=30, timezone=TIMEZONE), id="job_scan", replace_existing=True)
scheduler.add_job(weekly_recap, CronTrigger(day_of_week="sun", hour=18, minute=0, timezone=TIMEZONE), id="weekly_recap", replace_existing=True)
scheduler.add_job(daily_spend_report, CronTrigger(hour=21, minute=0, timezone=TIMEZONE), id="spend_report", replace_existing=True)
scheduler.add_job(evening_news, CronTrigger(hour=19, minute=0, timezone=TIMEZONE), id="evening_news", replace_existing=True)
scheduler.add_job(mood_checkin, CronTrigger(hour=21, minute=30, timezone=TIMEZONE), id="mood_checkin", replace_existing=True)
scheduler.add_job(weekly_savings, CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=TIMEZONE), id="weekly_savings", replace_existing=True)
scheduler.add_job(daily_spend_ask, CronTrigger(hour=22, minute=0, timezone=TIMEZONE), id="daily_spend_ask", replace_existing=True)
scheduler.add_job(weekly_spend_report, CronTrigger(day_of_week="tue", hour=16, minute=0, timezone=TIMEZONE), id="weekly_spend_report", replace_existing=True)

# Keep-alive — ping self every 5 minutes so Render never spins down
def keep_alive():
    try:
        requests.get("https://reed-ai-backend.onrender.com/ping", timeout=10)
        print("Keep-alive ping sent.")
    except Exception as e:
        print(f"Keep-alive error: {e}")

scheduler.add_job(keep_alive, "interval", minutes=5, id="keep_alive", replace_existing=True)
scheduler.start()
print(f"Scheduler started. Morning briefing at {BRIEFING_HOUR}:00 {TIMEZONE}")

@app.route("/")
def index():
    return jsonify({"status": "Reed AI Backend Running", "time": datetime.now().isoformat()})

@app.route("/ping")
def ping():
    return jsonify({"ok": True})

@app.route("/test-sms")
def test_sms():
    send_whatsapp("Reed AI Is Online And Running. Your Morning Briefings And Job Alerts Are Active. 🤖")
    return jsonify({"sent": True})

@app.route("/run-briefing")
def run_briefing_route():
    morning_briefing()
    return jsonify({"ran": "morning_briefing"})

@app.route("/run-jobs")
def run_jobs_route():
    job_scan()
    return jsonify({"ran": "job_scan"})

@app.route("/agent/scan-jobs", methods=["POST", "GET"])
def agent_scan_jobs():
    t = threading.Thread(target=run_agent_job_scan)
    t.daemon = True
    t.start()
    return jsonify({"started": True})

@app.route("/agent/briefing", methods=["POST", "GET"])
def agent_briefing():
    t = threading.Thread(target=run_agent_briefing)
    t.daemon = True
    t.start()
    return jsonify({"started": True})

@app.route("/agent/task", methods=["POST"])
def agent_task():
    data = request.get_json() or {}
    task_type = data.get("type", "custom")
    prompt = data.get("prompt", "")
    if not prompt and task_type == "custom":
        return jsonify({"error": "No prompt provided"}), 400
    t = threading.Thread(target=run_agent_task, args=(task_type, prompt))
    t.daemon = True
    t.start()
    return jsonify({"started": True})

@app.route("/agent/news", methods=["POST", "GET"])
def agent_news():
    t = threading.Thread(target=evening_news)
    t.daemon = True
    t.start()
    return jsonify({"started": True})

@app.route("/agent/checkin", methods=["POST", "GET"])
def agent_checkin():
    t = threading.Thread(target=mood_checkin)
    t.daemon = True
    t.start()
    return jsonify({"started": True})

@app.route("/agent/savings", methods=["POST", "GET"])
def agent_savings():
    t = threading.Thread(target=weekly_savings)
    t.daemon = True
    t.start()
    return jsonify({"started": True})

@app.route("/agent/savings/add", methods=["POST"])
def agent_savings_add():
    data = request.get_json() or {}
    amount = float(data.get("amount", 0))
    note = data.get("note", "")
    savings = load_savings()
    savings["total"] = savings.get("total", 0) + amount
    savings.setdefault("entries", []).append({
        "amount": amount,
        "note": note,
        "date": datetime.now().strftime("%Y-%m-%d")
    })
    save_savings(savings)
    return jsonify({"total": savings["total"], "added": amount})

@app.route("/agent/spend/log", methods=["POST"])
def agent_log_spend():
    data = request.get_json() or {}
    amount = float(data.get("amount", 0))
    note = data.get("note", "")
    spend_data = load_personal_spend()
    spend_data.setdefault("entries", []).append({
        "amount": amount,
        "note": note,
        "date": datetime.now().strftime("%Y-%m-%d")
    })
    save_personal_spend(spend_data)
    return jsonify({"logged": True, "amount": amount})

@app.route("/agent/spend/history", methods=["GET"])
def agent_spend_history():
    return jsonify(load_personal_spend())

@app.route("/agent/savings/get", methods=["GET"])
def agent_savings_get():
    return jsonify(load_savings())

@app.route("/agent/spend", methods=["GET"])
def agent_spend():
    data = load_spend()
    cost = calc_cost(data.get("input_tokens",0), data.get("output_tokens",0))
    return jsonify({**data, "estimated_cost_usd": round(cost, 4)})

@app.route("/agent/status", methods=["GET"])
def agent_status():
    return jsonify({
        "online": True,
        "scheduler": scheduler.state == STATE_RUNNING,
        "next_briefing": str(scheduler.get_job("morning_briefing").next_run_time) if scheduler.get_job("morning_briefing") else None,
        "next_job_scan": str(scheduler.get_job("job_scan").next_run_time) if scheduler.get_job("job_scan") else None,
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

