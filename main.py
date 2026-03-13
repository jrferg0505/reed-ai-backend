import os, json, re, requests, threading
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GRequest
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import STATE_RUNNING
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, jsonify, request
from flask_cors import CORS
from twilio.rest import Client

app = Flask(__name__)
CORS(app)

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY")
TWILIO_SID    = os.environ.get("TWILIO_SID")
TWILIO_TOKEN  = os.environ.get("TWILIO_TOKEN")
TWILIO_FROM   = os.environ.get("TWILIO_FROM")
REED_PHONE    = os.environ.get("REED_PHONE")
BRIEFING_HOUR = int(os.environ.get("BRIEFING_HOUR", "8"))
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI  = os.environ.get("GOOGLE_REDIRECT_URI", "https://reed-ai-backend.onrender.com/gcal/callback")
GCAL_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]
TIMEZONE      = os.environ.get("TIMEZONE", "America/New_York")

def load_json(path, default):
    try:
        with open(path) as f: return json.load(f)
    except: return default

def save_json(path, data):
    with open(path, "w") as f: json.dump(data, f)

def send_whatsapp(body):
    try:
        Client(TWILIO_SID, TWILIO_TOKEN).messages.create(
            body=body, from_="whatsapp:"+TWILIO_FROM, to="whatsapp:"+REED_PHONE)
        print(f"WhatsApp sent: {body[:60]}...")
    except Exception as e:
        print(f"WhatsApp error: {e}")

def ask_claude(prompt, use_search=False, max_tokens=1024):
    headers = {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    body = {"model": "claude-sonnet-4-5", "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
    if use_search:
        headers["anthropic-beta"] = "web-search-2025-03-05"
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
    data = r.json()
    if data.get("error"): raise Exception(data["error"]["message"])
    if data.get("stop_reason") == "tool_use" and use_search:
        r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json={
            "model": "claude-sonnet-4-5", "max_tokens": max_tokens,
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
            "messages": [{"role": "user", "content": prompt}, {"role": "assistant", "content": data["content"]}]})
        data = r.json()
    text = "".join(b["text"] for b in data.get("content", []) if b.get("type") == "text")
    try:
        usage = data.get("usage", {})
        spend = load_json("daily_api_spend.json", {"date": "", "input_tokens": 0, "output_tokens": 0, "calls": 0})
        today = datetime.now().strftime("%Y-%m-%d")
        if spend.get("date") != today:
            spend = {"date": today, "input_tokens": 0, "output_tokens": 0, "calls": 0}
        spend["input_tokens"] += usage.get("input_tokens", 0)
        spend["output_tokens"] += usage.get("output_tokens", 0)
        spend["calls"] += 1
        save_json("daily_api_spend.json", spend)
    except: pass
    return text.strip()

def morning_briefing():
    print("Running morning briefing...")
    try:
        cal_text = gcal_events_text(days=1)
        cal_section = f"\n📅 Today's Schedule:\n{cal_text}" if cal_text and cal_text != "No upcoming events." else ""
        text = ask_claude("""Search for Indianapolis weather today. Give Reed his morning briefing.
Format (under 400 chars, use line breaks):
Good Morning Reed 🌅
☀️ Weather: [Indianapolis weather today — temp + conditions]
💬 [One punchy motivational quote]
🎯 Focus: [One specific action toward his goals]
💼 Job Tip: [One quick job search tip]
Reed: hunting $18+/hr office job Indianapolis, saving for car, learning AI. Capitalize Every Word.""", use_search=True)
        send_whatsapp(text)
        print("Morning briefing sent.")
    except Exception as e:
        print(f"Morning briefing error: {e}")

def job_scan():
    print("Running job scan...")
    try:
        seen = set(load_json("seen_jobs.json", []))
        result = ask_claude("""Search Indeed LinkedIn ZipRecruiter Google Jobs for open office jobs in Indianapolis IN. No degree, $18+/hr, in-person M-F. Roles: office coordinator, admin assistant, customer success, inside sales, front desk, scheduling, operations, receptionist, data entry.
Return ONLY JSON array:
[{"title":"","company":"","location":"","pay":"","id":"slug"}]
Up to 5 real listings.""", use_search=True)
        match = re.search(r'\[.*\]', result, re.DOTALL)
        if not match: return
        jobs = json.loads(match.group())
        new_jobs = [j for j in jobs if j.get("id") not in seen]
        if not new_jobs: return
        for job in new_jobs[:3]:
            send_whatsapp(f"🔍 New Job!\n{job['title']} @ {job['company']}\n📍 {job['location']}\n💰 {job.get('pay','?')}\nOpen Reed AI To Save It.")
            seen.add(job.get("id"))
        save_json("seen_jobs.json", list(seen))
    except Exception as e:
        print(f"Job scan error: {e}")

def evening_news():
    print("Running evening news...")
    try:
        text = ask_claude("""Search for today's top AI and tech news.
Format (under 400 chars):
📡 Today In AI & Tech
1. [Headline + one sentence]
2. [Headline + one sentence]
3. [Headline + one sentence]
Capitalize Every Word.""", use_search=True)
        send_whatsapp(text)
        print("Evening news sent.")
    except Exception as e:
        print(f"Evening news error: {e}")

def mood_checkin():
    print("Running mood check-in...")
    try:
        day = datetime.now().strftime("%A")
        text = ask_claude(f"""Send Reed a quick end-of-day check-in. Today is {day}.
Under 200 chars. Casual, genuine.
Hey Reed 🌙
[One sentence asking how his day went]
Reply back and I'll listen.""")
        send_whatsapp(text)
        print("Mood check-in sent.")
    except Exception as e:
        print(f"Mood check-in error: {e}")

def weekly_recap():
    print("Running weekly recap...")
    try:
        text = ask_claude("""Write Reed a brief weekly job search motivation text. Under 280 chars.
📊 Weekly Check-In
[2 sentences: acknowledge grind, specific encouragement]
This Week: [One concrete action]""")
        send_whatsapp(text)
    except Exception as e:
        print(f"Weekly recap error: {e}")

def weekly_savings():
    print("Running weekly savings check...")
    try:
        data = load_json("savings.json", {"total": 0, "entries": []})
        total = data.get("total", 0)
        weeks = len(data.get("entries", []))
        msg = f"💰 Weekly Savings Check-In\nTotal Saved: ${total:.2f}\n"
        if weeks > 0: msg += f"Avg/Week: ${total/weeks:.2f}\n"
        msg += "\nHow Much Did You Save This Week? Reply With The Amount."
        send_whatsapp(msg)
    except Exception as e:
        print(f"Weekly savings error: {e}")

def daily_spend_ask():
    print("Running daily spend ask...")
    send_whatsapp("💸 Hey Reed — How Much Did You Spend Today?\nReply With Just A Number (E.g. '34' or '0').")

def weekly_spend_report():
    print("Running weekly spend report...")
    try:
        data = load_json("daily_personal_spend.json", {"entries": []})
        today = datetime.now().date()
        week = [e for e in data.get("entries", []) if (today - datetime.strptime(e["date"], "%Y-%m-%d").date()).days <= 7]
        if not week:
            send_whatsapp("📊 Weekly Spend: No Data Logged This Week.")
            return
        total = sum(e["amount"] for e in week)
        avg = total / len(week)
        hi = max(week, key=lambda e: e["amount"])
        msg = f"📊 Weekly Spend Report\nTotal: ${total:.2f} Over {len(week)} Days\nAvg/Day: ${avg:.2f}\nHighest: ${hi['amount']:.2f} ({hi['date']})\n"
        msg += "⚠️ High." if total > 300 else ("👀 Moderate." if total > 150 else "✅ Lean.")
        send_whatsapp(msg)
    except Exception as e:
        print(f"Weekly spend report error: {e}")

def daily_spend_report():
    print("Running daily API spend report...")
    try:
        data = load_json("daily_api_spend.json", {"date": "", "input_tokens": 0, "output_tokens": 0, "calls": 0})
        cost = (data.get("input_tokens", 0) / 1e6 * 3.0) + (data.get("output_tokens", 0) / 1e6 * 15.0)
        tokens = data.get("input_tokens", 0) + data.get("output_tokens", 0)
        cost_str = "< $0.01" if cost < 0.01 else f"${cost:.3f}"
        msg = f"💰 Daily API Report\nCalls: {data.get('calls',0)}\nTokens: {tokens:,}\nCost: {cost_str}\n"
        msg += ("⚠️ High Spend." if cost > 0.50 else ("✅ Normal." if cost > 0.10 else "✅ Minimal."))
        send_whatsapp(msg)
    except Exception as e:
        print(f"Daily spend report error: {e}")

def keep_alive():
    try:
        requests.get("https://reed-ai-backend.onrender.com/ping", timeout=10)
    except: pass

def run_agent_job_scan():
    try:
        send_whatsapp("🤖 Starting Job Scan Now. I'll Text You What I Find...")
        result = ask_claude("""Search Indeed LinkedIn ZipRecruiter Google Jobs for open office jobs Indianapolis IN. No degree, $18+/hr, in-person M-F.
Return ONLY JSON:
[{"title":"","company":"","location":"","pay":"","applyUrl":"","id":""}]
Up to 5 real listings.""", use_search=True, max_tokens=2048)
        match = re.search(r'\[.*\]', result, re.DOTALL)
        if not match:
            send_whatsapp("❌ Couldn't Find Jobs Right Now. Try Again Later.")
            return
        jobs = json.loads(match.group())
        if not jobs:
            send_whatsapp("😕 No Jobs Found Right Now. Checking Every 6 Hours.")
            return
        send_whatsapp(f"✅ Found {len(jobs)} Jobs:")
        for job in jobs[:5]:
            msg = f"💼 {job['title']}\n🏢 {job['company']}\n📍 {job['location']}\n💰 {job.get('pay','?')}"
            if job.get('applyUrl'): msg += f"\n🔗 {job['applyUrl']}"
            send_whatsapp(msg)
    except Exception as e:
        send_whatsapp(f"❌ Error: {str(e)[:100]}")

def run_agent_task(task_type, custom_prompt=None):
    try:
        if task_type == "job_scan": run_agent_job_scan()
        elif task_type == "briefing": morning_briefing()
        elif task_type == "news": evening_news()
        elif task_type == "checkin": mood_checkin()
        elif task_type == "custom" and custom_prompt:
            send_whatsapp("🤖 Working On It...")
            result = ask_claude(f"You are Reed's AI. Reed: Indianapolis, donut shop, hunting $18+/hr office job, saving for car, learning AI. Direct, capitalize every word, under 500 chars.\nTask: {custom_prompt}", use_search=True)
            send_whatsapp(f"✅ Done:\n{result}")
    except Exception as e:
        send_whatsapp(f"❌ Error: {str(e)[:100]}")

# ── SCHEDULER ──
scheduler = BackgroundScheduler(timezone=TIMEZONE)
scheduler.add_job(morning_briefing,    CronTrigger(hour=BRIEFING_HOUR, minute=0, timezone=TIMEZONE),        id="morning_briefing",    replace_existing=True)
scheduler.add_job(job_scan,            CronTrigger(hour=9, minute=0, timezone=TIMEZONE),                    id="job_scan",            replace_existing=True)
scheduler.add_job(evening_news,        CronTrigger(hour=19, minute=0, timezone=TIMEZONE),                   id="evening_news",        replace_existing=True)
scheduler.add_job(mood_checkin,        CronTrigger(hour=21, minute=30, timezone=TIMEZONE),                  id="mood_checkin",        replace_existing=True)
scheduler.add_job(weekly_recap,        CronTrigger(day_of_week="sun", hour=18, minute=0, timezone=TIMEZONE),id="weekly_recap",        replace_existing=True)
scheduler.add_job(weekly_savings,      CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=TIMEZONE),id="weekly_savings",      replace_existing=True)
scheduler.add_job(daily_spend_ask,     CronTrigger(hour=22, minute=0, timezone=TIMEZONE),                   id="daily_spend_ask",     replace_existing=True)
scheduler.add_job(weekly_spend_report, CronTrigger(day_of_week="tue", hour=16, minute=0, timezone=TIMEZONE),id="weekly_spend_report", replace_existing=True)
scheduler.add_job(daily_spend_report,  CronTrigger(hour=21, minute=0, timezone=TIMEZONE),                   id="daily_spend_report",  replace_existing=True)
scheduler.add_job(keep_alive,          "interval", minutes=5,                                               id="keep_alive",          replace_existing=True)
scheduler.start()
print(f"Scheduler started. Morning briefing at {BRIEFING_HOUR}:00 {TIMEZONE}")

# ── ROUTES ──
# ── Google Calendar helpers ──
def gcal_token_path():
    return "gcal_token.json"

def get_gcal_creds():
    path = gcal_token_path()
    if not os.path.exists(path):
        return None
    creds = Credentials.from_authorized_user_file(path, GCAL_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(GRequest())
            with open(path, "w") as f:
                f.write(creds.to_json())
        except Exception as e:
            print(f"Token refresh error: {e}")
            return None
    return creds if (creds and creds.valid) else None

def get_gcal_service():
    creds = get_gcal_creds()
    if not creds:
        return None
    return build("calendar", "v3", credentials=creds)

def gcal_events_text(days=3):
    """Get upcoming events as plain text for briefings."""
    try:
        svc = get_gcal_service()
        if not svc:
            return ""
        now = datetime.utcnow().isoformat() + "Z"
        end = (datetime.utcnow() + timedelta(days=days)).isoformat() + "Z"
        result = svc.events().list(
            calendarId="primary", timeMin=now, timeMax=end,
            maxResults=10, singleEvents=True, orderBy="startTime"
        ).execute()
        events = result.get("items", [])
        if not events:
            return "No upcoming events."
        lines = []
        for e in events:
            start = e["start"].get("dateTime", e["start"].get("date", ""))
            try:
                dt = datetime.fromisoformat(start.replace("Z",""))
                fmt = dt.strftime("%a %b %d, %I:%M %p")
            except:
                fmt = start
            lines.append(f"- {fmt}: {e.get('summary','(no title)')}")
        return "\n".join(lines)
    except Exception as e:
        print(f"gcal_events_text error: {e}")
        return ""

@app.route("/")
def index(): return jsonify({"status": "Reed AI Backend Running", "time": datetime.now().isoformat()})

@app.route("/chat", methods=["POST"])
def chat():
    try:
        data = request.json
        messages = data.get("messages", [])
        system = data.get("system", "")
        use_search = data.get("use_search", False)
        model = data.get("model", "claude-haiku-4-5-20251001")
        max_tokens = data.get("max_tokens", 512)
        # Accept key from frontend if env var missing
        api_key = ANTHROPIC_KEY or data.get("api_key", "")
        if not api_key:
            return jsonify({"error": "No API key configured"}), 400
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        body = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if system: body["system"] = system
        if use_search:
            headers["anthropic-beta"] = "web-search-2025-03-05"
            body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
        r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
        result = r.json()
        if result.get("error"): return jsonify({"error": result["error"]["message"]}), 400
        # Handle tool use (web search)
        if result.get("stop_reason") == "tool_use" and use_search:
            r2 = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json={
                "model": model, "max_tokens": max_tokens, "system": system if system else "",
                "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
                "messages": messages + [{"role": "assistant", "content": result["content"]}]
            })
            result = r2.json()
            if result.get("error"): return jsonify({"error": result["error"]["message"]}), 400
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/ping")
def ping(): return jsonify({"ok": True})

@app.route("/test-sms")
def test_sms():
    send_whatsapp("Reed AI Is Online And Running. 🤖")
    return jsonify({"sent": True})

@app.route("/agent/status")
def agent_status():
    return jsonify({"online": True, "scheduler": scheduler.state == STATE_RUNNING,
        "next_briefing": str(scheduler.get_job("morning_briefing").next_run_time),
        "next_job_scan": str(scheduler.get_job("job_scan").next_run_time)})

@app.route("/agent/spend")
def agent_spend():
    data = load_json("daily_api_spend.json", {"date":"","input_tokens":0,"output_tokens":0,"calls":0})
    cost = (data.get("input_tokens",0)/1e6*3.0) + (data.get("output_tokens",0)/1e6*15.0)
    return jsonify({**data, "estimated_cost_usd": round(cost, 4)})

@app.route("/agent/scan-jobs", methods=["POST","GET"])
def agent_scan_jobs():
    threading.Thread(target=run_agent_job_scan, daemon=True).start()
    return jsonify({"started": True})

@app.route("/agent/briefing", methods=["POST","GET"])
def agent_briefing():
    threading.Thread(target=morning_briefing, daemon=True).start()
    return jsonify({"started": True})

@app.route("/agent/news", methods=["POST","GET"])
def agent_news():
    threading.Thread(target=evening_news, daemon=True).start()
    return jsonify({"started": True})

@app.route("/agent/checkin", methods=["POST","GET"])
def agent_checkin():
    threading.Thread(target=mood_checkin, daemon=True).start()
    return jsonify({"started": True})

@app.route("/agent/task", methods=["POST"])
def agent_task():
    data = request.get_json() or {}
    task_type = data.get("type", "custom")
    prompt = data.get("prompt", "")
    if not prompt and task_type == "custom":
        return jsonify({"error": "No prompt"}), 400
    threading.Thread(target=run_agent_task, args=(task_type, prompt), daemon=True).start()
    return jsonify({"started": True})

@app.route("/agent/savings/add", methods=["POST"])
def agent_savings_add():
    data = request.get_json() or {}
    amount = float(data.get("amount", 0))
    sav = load_json("savings.json", {"total": 0, "entries": []})
    sav["total"] = sav.get("total", 0) + amount
    sav.setdefault("entries", []).append({"amount": amount, "note": data.get("note",""), "date": datetime.now().strftime("%Y-%m-%d")})
    save_json("savings.json", sav)
    return jsonify({"total": sav["total"], "added": amount})

@app.route("/agent/savings/get")
def agent_savings_get():
    return jsonify(load_json("savings.json", {"total": 0, "entries": []}))

@app.route("/agent/spend/log", methods=["POST"])
def agent_log_spend():
    data = request.get_json() or {}
    amount = float(data.get("amount", 0))
    spend_data = load_json("daily_personal_spend.json", {"entries": []})
    spend_data.setdefault("entries", []).append({"amount": amount, "note": data.get("note",""), "date": datetime.now().strftime("%Y-%m-%d")})
    save_json("daily_personal_spend.json", spend_data)
    return jsonify({"logged": True, "amount": amount})

@app.route("/agent/spend/history")
def agent_spend_history():
    return jsonify(load_json("daily_personal_spend.json", {"entries": []}))

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

# ── WhatsApp two-way chat ──
# Incoming messages from Twilio webhook are stored here
# Frontend polls /wa-messages and sends via /wa-send

@app.route("/wa-webhook", methods=["POST"])
def wa_webhook():
    """Twilio sends incoming WhatsApp messages here."""
    import uuid
    from_num = request.form.get("From", "")
    body     = request.form.get("Body", "").strip()
    if not body:
        return ("", 204)
    msgs = load_json("wa_inbox.json", [])
    msg = {
        "id":   str(uuid.uuid4()),
        "from": from_num,
        "body": body,
        "ts":   int(datetime.now().timestamp() * 1000)
    }
    msgs.append(msg)
    # keep last 500
    if len(msgs) > 500:
        msgs = msgs[-500:]
    save_json("wa_inbox.json", msgs)
    print(f"WA in from {from_num}: {body[:60]}")
    return ("", 204)

@app.route("/wa-messages")
def wa_messages():
    """Return messages newer than `since` (a message id string).
    If since is empty, returns last 50 messages."""
    since_id = request.args.get("since", "")
    msgs = load_json("wa_inbox.json", [])
    if since_id:
        # find index of last known id and return everything after
        idx = next((i for i,m in enumerate(msgs) if m["id"]==since_id), None)
        if idx is not None:
            msgs = msgs[idx+1:]
        else:
            msgs = msgs[-50:]
    else:
        msgs = msgs[-50:]
    return jsonify({"messages": msgs})

@app.route("/wa-send", methods=["POST"])
def wa_send():
    """Send a WhatsApp message to Reed's phone (or a specified number)."""
    data    = request.get_json() or {}
    body    = data.get("message", "").strip()
    to_num  = data.get("to", "").strip() or REED_PHONE
    if not body:
        return jsonify({"error": "No message"}), 400
    try:
        # ensure +1 format
        if not to_num.startswith("+"):
            to_num = "+" + to_num.replace(" ", "")
        Client(TWILIO_SID, TWILIO_TOKEN).messages.create(
            body=body,
            from_="whatsapp:" + TWILIO_FROM,
            to="whatsapp:" + to_num
        )
        return jsonify({"sent": True})
    except Exception as e:
        print(f"WA send error: {e}")
        return jsonify({"error": str(e)}), 500

# ── Google Calendar Routes ──

@app.route("/gcal/auth")
def gcal_auth():
    """Start OAuth flow — redirect user to Google consent screen."""
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return jsonify({"error": "Google credentials not configured in Render env vars"}), 400
    flow = Flow.from_client_config(
        {"web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [GOOGLE_REDIRECT_URI]
        }},
        scopes=GCAL_SCOPES,
        redirect_uri=GOOGLE_REDIRECT_URI
    )
    flow.code_verifier = None  # disable PKCE — server-side flow doesn't need it
    auth_url, _ = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent"
    )
    return jsonify({"auth_url": auth_url})

@app.route("/gcal/callback")
def gcal_callback():
    """Google redirects here after consent. Save token."""
    code = request.args.get("code")
    if not code:
        return "<h2>Error: no code returned from Google</h2>", 400
    # Exchange code directly via HTTP POST — bypasses Flow/PKCE entirely
    token_resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
    )
    token_data = token_resp.json()
    if "error" in token_data:
        return f"<h2>Token exchange error: {token_data.get('error_description', token_data.get('error'))}</h2>", 400
    creds = Credentials(
        token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=GCAL_SCOPES,
    )
    with open(gcal_token_path(), "w") as f:
        f.write(creds.to_json())
    return """<html><body style='background:#000;color:#fff;font-family:-apple-system,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;flex-direction:column;gap:16px;'>
    <div style='font-size:48px;'>✓</div>
    <div style='font-size:20px;font-weight:600;'>Google Calendar Connected</div>
    <div style='color:rgba(255,255,255,0.5);font-size:15px;'>You can close this tab and return to Onyx.</div>
    </body></html>"""

@app.route("/gcal/status")
def gcal_status():
    creds = get_gcal_creds()
    if not creds:
        return jsonify({"connected": False})
    try:
        svc = get_gcal_service()
        cal = svc.calendars().get(calendarId="primary").execute()
        return jsonify({"connected": True, "email": cal.get("id","")})
    except:
        return jsonify({"connected": False})

@app.route("/gcal/events")
def gcal_events():
    """Get upcoming events. ?days=7 optional."""
    days = int(request.args.get("days", 7))
    try:
        svc = get_gcal_service()
        if not svc:
            return jsonify({"error": "not_connected"}), 401
        now = datetime.utcnow().isoformat() + "Z"
        end = (datetime.utcnow() + timedelta(days=days)).isoformat() + "Z"
        result = svc.events().list(
            calendarId="primary", timeMin=now, timeMax=end,
            maxResults=25, singleEvents=True, orderBy="startTime"
        ).execute()
        events = []
        for e in result.get("items", []):
            start = e["start"].get("dateTime", e["start"].get("date",""))
            end_t = e["end"].get("dateTime", e["end"].get("date",""))
            events.append({
                "id":       e["id"],
                "title":    e.get("summary","(no title)"),
                "start":    start,
                "end":      end_t,
                "location": e.get("location",""),
                "desc":     e.get("description",""),
                "link":     e.get("htmlLink",""),
                "allDay":   "T" not in e["start"].get("dateTime","T")
            })
        return jsonify({"events": events})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/gcal/create", methods=["POST"])
def gcal_create():
    """Create a new event."""
    data = request.get_json() or {}
    try:
        svc = get_gcal_service()
        if not svc:
            return jsonify({"error": "not_connected"}), 401
        event_body = {
            "summary": data.get("title", "New Event"),
            "location": data.get("location", ""),
            "description": data.get("desc", ""),
            "start": {"dateTime": data["start"], "timeZone": TIMEZONE},
            "end":   {"dateTime": data["end"],   "timeZone": TIMEZONE},
        }
        created = svc.events().insert(calendarId="primary", body=event_body).execute()
        return jsonify({"created": True, "id": created["id"], "link": created.get("htmlLink","")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/gcal/update", methods=["POST"])
def gcal_update():
    """Update an existing event."""
    data = request.get_json() or {}
    event_id = data.get("id")
    if not event_id:
        return jsonify({"error": "No event id"}), 400
    try:
        svc = get_gcal_service()
        if not svc:
            return jsonify({"error": "not_connected"}), 401
        event = svc.events().get(calendarId="primary", eventId=event_id).execute()
        if "title" in data:   event["summary"]     = data["title"]
        if "location" in data: event["location"]    = data["location"]
        if "desc" in data:    event["description"] = data["desc"]
        if "start" in data:   event["start"]       = {"dateTime": data["start"], "timeZone": TIMEZONE}
        if "end" in data:     event["end"]         = {"dateTime": data["end"],   "timeZone": TIMEZONE}
        updated = svc.events().update(calendarId="primary", eventId=event_id, body=event).execute()
        return jsonify({"updated": True, "id": updated["id"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/gcal/delete", methods=["POST"])
def gcal_delete():
    """Delete an event."""
    data = request.get_json() or {}
    event_id = data.get("id")
    if not event_id:
        return jsonify({"error": "No event id"}), 400
    try:
        svc = get_gcal_service()
        if not svc:
            return jsonify({"error": "not_connected"}), 401
        svc.events().delete(calendarId="primary", eventId=event_id).execute()
        return jsonify({"deleted": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/gcal/disconnect", methods=["POST"])
def gcal_disconnect():
    path = gcal_token_path()
    if os.path.exists(path):
        os.remove(path)
    return jsonify({"disconnected": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
