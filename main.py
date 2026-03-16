import os, json, re, requests, threading, base64
from email.mime.text import MIMEText
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
    "https://www.googleapis.com/auth/documents.readonly",
]
GMAIL_REDIRECT_URI = os.environ.get("GMAIL_REDIRECT_URI", "https://reed-ai-backend.onrender.com/gmail/callback")
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
TIMEZONE      = os.environ.get("TIMEZONE", "America/New_York")
ONYX_API_KEY  = os.environ.get("ONYX_API_KEY", "")
HOURLY_RATE   = float(os.environ.get("HOURLY_RATE", "0"))
GOVEE_API_KEY  = os.environ.get("GOVEE_API_KEY", "")
GOVEE_BASE     = "https://developer-api.govee.com/v1/devices"
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# Routes exempt from API key check (Twilio + Google OAuth callbacks must be public)
_PUBLIC_ROUTES = {"/wa-webhook", "/gcal/callback", "/gmail/callback", "/health", "/ping"}

@app.before_request
def require_api_key():
    if request.method == "OPTIONS":
        return  # let CORS preflight through
    if request.path in _PUBLIC_ROUTES:
        return  # public endpoints
    if not ONYX_API_KEY:
        return  # key not configured yet — fail open so existing setup isn't broken
    provided = request.headers.get("X-API-Key", "")
    if provided != ONYX_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

PLAID_CLIENT_ID = os.environ.get("PLAID_CLIENT_ID", "")
PLAID_SECRET    = os.environ.get("PLAID_SECRET", "")
PLAID_ENV       = os.environ.get("PLAID_ENV", "sandbox")
PLAID_BASE_URL  = f"https://{PLAID_ENV}.plaid.com"

# In-memory store for pending email drafts triggered via WhatsApp
# { from_num: { "to_name", "to_email", "subject", "body", "waiting_for" } }
wa_pending_emails = {}

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

def _wa_send_email(to_addr, subject, body_text):
    """Send an email via the stored Gmail credentials.
    Returns (True, None) on success or (False, error_string) on failure."""
    try:
        svc = get_gmail_service()
        if not svc:
            print("[WA SEND EMAIL] no Gmail service — token missing or expired")
            return False, "Gmail not connected — connect it in the Onyx app first"
        mime_msg = MIMEText(body_text)
        mime_msg["to"]      = to_addr
        mime_msg["subject"] = subject
        raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
        svc.users().messages().send(userId="me", body={"raw": raw}).execute()
        print(f"[WA SEND EMAIL] sent to {to_addr} / subject: {subject}")
        return True, None
    except Exception as e:
        print(f"[WA SEND EMAIL] error: {e}")
        return False, str(e)

def _wa_parse_email_intent(text):
    """Detect and parse an email-send intent from a WhatsApp message.

    Strategy (no single point of failure):
      1. Keyword pre-filter — bail fast if no email trigger word.
      2. Regex fast-path — extract address + body directly from text.
         Returns immediately if we have everything; no API call needed.
      3. Claude fallback — only called when info is genuinely missing
         (e.g. recipient is a name, not an address).  If the API call
         fails for any reason we fall back to whatever regex found.

    Returns a dict compatible with Path B in _auto_reply, or None.
    """
    # ── 1. Pre-filter ──────────────────────────────────────────────────────
    if not re.search(
        r'\b(email|e-mail|send\s+(an?\s+)?email|write\s+to|message\s+to)\b',
        text, re.IGNORECASE
    ):
        return None

    # ── 2. Regex fast-path ─────────────────────────────────────────────────
    # Extract email address if present
    addr_m = re.search(r'[\w.+\-]+@[\w\-]+\.[a-zA-Z]{2,}', text)
    to_email_regex = addr_m.group() if addr_m else None

    # Extract body: everything after keywords like "saying", "tell X that", etc.
    body_regex = None
    body_m = re.search(
        r'\b(?:saying|say(?:ing)?|tell\s+\w+(?:\s+that)?|that\s+(?=\S))\s+(.+)$',
        text, re.IGNORECASE | re.DOTALL
    )
    if body_m:
        body_regex = body_m.group(1).strip()
    else:
        # Fallback: everything after the email address (minus connector words)
        if to_email_regex:
            after = text[text.find(to_email_regex) + len(to_email_regex):].strip()
            after = re.sub(r'^(?:and\s+)?(?:to\s+)?(?:say|tell\s+\w+)\s*', '', after,
                           flags=re.IGNORECASE).strip()
            # "about X" is a topic — need to ask for the actual message
            if after and not re.match(r'^about\b', after, re.IGNORECASE):
                body_regex = after

    # Build subject from first ~60 chars of body, or a generic fallback
    def _make_subject(b):
        if not b:
            return "Message from Reed"
        s = b.strip().rstrip('.!?')
        return (s[:57] + "...") if len(s) > 60 else s

    # If regex gave us both address and body → return immediately (no API call)
    if to_email_regex and body_regex:
        result = {
            "is_email": True,
            "to_email": to_email_regex,
            "to_name":  None,
            "subject":  _make_subject(body_regex),
            "body":     body_regex,
            "missing":  "none",
        }
        print(f"[WA EMAIL PARSE] fast-path: {result}")
        return result

    # If regex gave address but no body → store partial and skip Claude
    if to_email_regex and not body_regex:
        result = {
            "is_email": True,
            "to_email": to_email_regex,
            "to_name":  None,
            "subject":  "Message from Reed",
            "body":     None,
            "missing":  "message_body",
        }
        print(f"[WA EMAIL PARSE] fast-path (need body): {result}")
        return result

    # ── 3. Claude fallback (name-only recipient or ambiguous) ──────────────
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 300,
                "system": (
                    "Extract email-send intent. "
                    "Reply with ONLY a raw JSON object — no markdown, no fences."
                ),
                "messages": [{"role": "user", "content":
                    f'Message: "{text}"\n'
                    'JSON only: {"is_email":true/false,"to_email":"addr or null",'
                    '"to_name":"name or null","subject":"subject or null",'
                    '"body":"email body or null","missing":"none|email_address|message_body|both"}'
                }],
            },
            timeout=12,
        )
        resp_data = r.json()
        raw_out = "".join(
            b["text"] for b in resp_data.get("content", []) if b.get("type") == "text"
        ).strip()
        print(f"[WA EMAIL PARSE] Claude raw: {raw_out[:300]}")

        cleaned = re.sub(r'^```(?:json)?\s*|\s*```\s*$', '', raw_out).strip()
        json_m = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if json_m:
            parsed = json.loads(json_m.group())
            print(f"[WA EMAIL PARSE] Claude parsed: {parsed}")
            if parsed.get("is_email"):
                return parsed
    except Exception as e:
        print(f"[WA EMAIL PARSE] Claude error (using regex fallback): {e}")

    # If Claude failed but we at least know it's an email intent (pre-filter passed
    # and text contained "email"), return a minimal result so Path B handles it.
    if to_email_regex:
        return {
            "is_email": True, "to_email": to_email_regex, "to_name": None,
            "subject": "Message from Reed", "body": None, "missing": "message_body",
        }
    # Couldn't extract enough — let the regex say "need email address"
    name_m = re.search(
        r'\bemail\s+(\w+)(?:\s+about|\s+saying|\s+to\s+say)?', text, re.IGNORECASE
    )
    if name_m:
        return {
            "is_email": True, "to_email": None,
            "to_name": name_m.group(1).capitalize(),
            "subject": None, "body": None, "missing": "email_address",
        }
    return None

def ask_claude(prompt, use_search=False, max_tokens=1024, timeout=25):
    headers = {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    body = {"model": "claude-sonnet-4-5", "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
    if use_search:
        headers["anthropic-beta"] = "web-search-2025-03-05"
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=timeout)
    data = r.json()
    if data.get("error"): raise Exception(data["error"]["message"])
    if data.get("stop_reason") == "tool_use" and use_search:
        r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json={
            "model": "claude-sonnet-4-5", "max_tokens": max_tokens,
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
            "messages": [{"role": "user", "content": prompt}, {"role": "assistant", "content": data["content"]}]},
            timeout=timeout)
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

def build_daily_briefing():
    """Build a personalized daily briefing string from live data (weather, calendar, email)."""
    try:
        today_str = datetime.now().strftime("%A, %B %-d")

        # 1. Weather via wttr.in — no API key needed
        try:
            weather_raw = requests.get(
                "https://wttr.in/Indianapolis?format=3", timeout=5
            ).text.strip()
            weather_line = f"☀️ {weather_raw}"
        except Exception:
            weather_line = "☀️ Weather unavailable right now"

        # 2. Google Calendar events today
        cal_text = gcal_events_text(days=1)
        if cal_text and cal_text.strip() and cal_text.strip() != "No upcoming events.":
            cal_section = "📅 Today's Schedule:\n" + cal_text.strip()
        else:
            cal_section = "📅 Nothing on the calendar today"

        # 3. Important emails from the last 24 hours
        email_section = ""
        try:
            svc = get_gmail_service()
            if svc:
                after_date = (datetime.now() - timedelta(hours=24)).strftime("%Y/%m/%d")
                query = (
                    f"after:{after_date} label:important "
                    "(subject:interview OR subject:offer OR subject:urgent "
                    "OR subject:invoice OR subject:recruiter OR subject:application)"
                )
                result = svc.users().messages().list(
                    userId="me", q=query, maxResults=5
                ).execute()
                msgs_found = result.get("messages", [])
                if msgs_found:
                    lines = []
                    for m in msgs_found[:3]:
                        hdrs = (
                            svc.users().messages().get(
                                userId="me", id=m["id"],
                                format="metadata",
                                metadataHeaders=["Subject", "From"],
                            )
                            .execute()
                            .get("payload", {})
                            .get("headers", [])
                        )
                        subj = next(
                            (h["value"] for h in hdrs if h["name"] == "Subject"),
                            "No Subject",
                        )[:55]
                        sender = next(
                            (h["value"] for h in hdrs if h["name"] == "From"),
                            "",
                        )
                        sender_name = re.sub(r'\s*<[^>]+>', '', sender).strip()[:30]
                        lines.append(f"  • {subj} — {sender_name}")
                    email_section = "📬 Important Emails:\n" + "\n".join(lines)
        except Exception as exc:
            print(f"Briefing email scan error: {exc}")

        # 4. Short motivational line via Claude
        try:
            motivation = ask_claude(
                "One short motivational line for Reed (Indianapolis, hunting office job, saving for a car). "
                "Under 80 chars. No quotes or attribution. Just the line.",
                use_search=False,
            ).strip()
        except Exception:
            motivation = "Keep going — every step forward counts."

        # 5. Assemble
        parts = [
            f"Good Morning Reed 🌅 — {today_str}",
            weather_line,
            cal_section,
        ]
        if email_section:
            parts.append(email_section)
        parts.append(f"💪 {motivation}")

        return "\n\n".join(parts)

    except Exception as e:
        print(f"build_daily_briefing error: {e}")
        return "Good morning Reed! 🌅 Couldn't pull your briefing right now — try again in a bit."


def morning_briefing():
    """Send on-demand daily briefing via WhatsApp (also called by /agent/briefing route)."""
    print("Sending daily briefing...")
    try:
        text = build_daily_briefing()
        send_whatsapp(text)
        print("Daily briefing sent.")
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
scheduler.add_job(job_scan,            CronTrigger(hour=9, minute=0, timezone=TIMEZONE),                    id="job_scan",            replace_existing=True)
scheduler.add_job(evening_news,        CronTrigger(hour=19, minute=0, timezone=TIMEZONE),                   id="evening_news",        replace_existing=True)
scheduler.add_job(mood_checkin,        CronTrigger(hour=21, minute=30, timezone=TIMEZONE),                  id="mood_checkin",        replace_existing=True)
scheduler.add_job(weekly_recap,        CronTrigger(day_of_week="sun", hour=18, minute=0, timezone=TIMEZONE),id="weekly_recap",        replace_existing=True)
scheduler.add_job(weekly_savings,      CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=TIMEZONE),id="weekly_savings",      replace_existing=True)
scheduler.add_job(daily_spend_ask,     CronTrigger(hour=22, minute=0, timezone=TIMEZONE),                   id="daily_spend_ask",     replace_existing=True)
scheduler.add_job(weekly_spend_report, CronTrigger(day_of_week="tue", hour=16, minute=0, timezone=TIMEZONE),id="weekly_spend_report", replace_existing=True)
scheduler.add_job(daily_spend_report,  CronTrigger(hour=21, minute=0, timezone=TIMEZONE),                   id="daily_spend_report",  replace_existing=True)
scheduler.add_job(keep_alive,            "interval", minutes=5,                                               id="keep_alive",            replace_existing=True)
scheduler.start()
print(f"Scheduler started ({TIMEZONE}). On-demand briefing active.")

# ── ROUTES ──
# ── Persistent token store ──
# Primary store: os.environ (in-process, survives Render spin-down within same process).
# Seeded from GCAL_TOKEN / GMAIL_TOKEN env vars set in the Render dashboard.
# After first OAuth, copy the logged token JSON into those env vars once — tokens
# then survive every future redeploy automatically because the refresh_token never expires.
# Local dev fallback: gcal_token.json / gmail_token.json files still work.

def _save_token(env_key, token_json):
    """Write token to os.environ (in-process) and to file (local dev fallback)."""
    os.environ[env_key] = token_json
    file_path = "gcal_token.json" if env_key == "GCAL_TOKEN" else "gmail_token.json"
    try:
        with open(file_path, "w") as f:
            f.write(token_json)
    except Exception:
        pass
    print(f"[TOKEN] {env_key} = {token_json}")

def _load_token(env_key, file_path):
    """Read token from os.environ first, then file fallback. Caches file value into os.environ."""
    val = os.environ.get(env_key, "")
    if val:
        return val
    if os.path.exists(file_path):
        try:
            with open(file_path) as f:
                token_json = f.read().strip()
            if token_json:
                os.environ[env_key] = token_json  # promote to env for faster future reads
                return token_json
        except Exception:
            pass
    return ""

def _clear_token(env_key, file_path):
    """Remove token from os.environ and delete file."""
    os.environ.pop(env_key, None)
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except Exception:
            pass

# ── Govee helpers ──
_GOVEE_COLORS = {
    "red": (255,0,0), "green": (0,255,0), "blue": (0,0,255),
    "white": (255,255,255), "warm white": (255,200,100), "yellow": (255,220,0),
    "orange": (255,100,0), "purple": (128,0,255), "pink": (255,20,147),
    "cyan": (0,255,255), "teal": (0,128,128), "lime": (0,255,0),
    "magenta": (255,0,255), "off": None,
}

def _govee_headers():
    return {"Govee-API-Key": GOVEE_API_KEY, "Content-Type": "application/json"}

def govee_get_devices():
    """Return list of Govee devices or empty list on error."""
    if not GOVEE_API_KEY:
        return []
    try:
        r = requests.get(GOVEE_BASE, headers=_govee_headers(), timeout=10)
        return r.json().get("data", {}).get("devices", [])
    except Exception as e:
        print(f"[GOVEE] get_devices error: {e}")
        return []

def govee_control_all(cmd_name, cmd_value):
    """Send a control command to ALL controllable Govee devices."""
    devices = govee_get_devices()
    if not devices:
        return False, "No Govee devices found or API key not set."
    ok = 0
    for dev in devices:
        if not dev.get("controllable", True):
            continue
        try:
            payload = {"device": dev["device"], "model": dev["model"],
                       "cmd": {"name": cmd_name, "value": cmd_value}}
            r = requests.put(f"{GOVEE_BASE}/control", headers=_govee_headers(),
                             json=payload, timeout=10)
            if r.status_code == 200:
                ok += 1
        except Exception as e:
            print(f"[GOVEE] control error on {dev.get('device')}: {e}")
    return ok > 0, f"Sent to {ok}/{len(devices)} devices."

def parse_govee_command(text):
    """Parse a light command string. Returns list of (cmd_name, cmd_value) tuples."""
    t = text.lower()
    cmds = []
    # on/off
    if re.search(r'\b(turn\s+)?(lights?\s+)?(on)\b', t) and "off" not in t:
        cmds.append(("turn", "on"))
    elif re.search(r'\b(turn\s+)?(lights?\s+)?off\b', t):
        cmds.append(("turn", "off"))
    # color
    for name, rgb in _GOVEE_COLORS.items():
        if name in t and rgb is not None:
            cmds.append(("color", {"r": rgb[0], "g": rgb[1], "b": rgb[2]}))
            break
    # hex color #RRGGBB
    hex_m = re.search(r'#([0-9a-fA-F]{6})', text)
    if hex_m:
        h = hex_m.group(1)
        cmds.append(("color", {"r": int(h[0:2],16), "g": int(h[2:4],16), "b": int(h[4:6],16)}))
    # brightness
    bri_m = re.search(r'(\d+)\s*%?\s*(brightness|bright|dim)', t) or \
            re.search(r'brightness\s+(?:to\s+)?(\d+)', t) or \
            re.search(r'set\s+(?:it\s+)?to\s+(\d+)%', t)
    if bri_m:
        val = int(bri_m.group(1))
        cmds.append(("brightness", max(1, min(100, val))))
    return cmds

def govee_wa_reply(text):
    """Execute parsed Govee command and return a WhatsApp reply string."""
    cmds = parse_govee_command(text)
    if not cmds:
        return None
    if not GOVEE_API_KEY:
        return "💡 Govee API key not set — add GOVEE_API_KEY in Render settings."
    msgs = []
    for name, value in cmds:
        ok, detail = govee_control_all(name, value)
        if name == "turn":
            msgs.append(f"💡 Lights {'on' if value=='on' else 'off'}. {detail}")
        elif name == "color":
            color_str = next((k for k,v in _GOVEE_COLORS.items() if v and tuple(value.values())==v), str(value))
            msgs.append(f"🎨 Color set to {color_str}. {detail}")
        elif name == "brightness":
            msgs.append(f"🔆 Brightness set to {value}%. {detail}")
    return "\n".join(msgs) if msgs else None

# ── Plaid helpers ──
def _plaid_post(path, payload):
    """POST to Plaid API with credentials injected."""
    r = requests.post(
        PLAID_BASE_URL + path,
        json={**payload, "client_id": PLAID_CLIENT_ID, "secret": PLAID_SECRET},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    return r.json()

def get_plaid_access_token():
    """Load the stored Plaid access token (env → file fallback)."""
    val = os.environ.get("PLAID_ACCESS_TOKEN", "")
    if val:
        return val
    try:
        with open("plaid_token.txt") as f:
            val = f.read().strip()
        if val:
            os.environ["PLAID_ACCESS_TOKEN"] = val
        return val
    except Exception:
        return ""

def save_plaid_access_token(token):
    os.environ["PLAID_ACCESS_TOKEN"] = token
    try:
        with open("plaid_token.txt", "w") as f:
            f.write(token)
    except Exception:
        pass
    print(f"[PLAID] access token saved")

def plaid_get_balance_text():
    """Return a plain-text balance summary for WhatsApp/briefing use."""
    access_token = get_plaid_access_token()
    if not access_token:
        return "Bank not connected yet. Open Onyx and connect via the Finance panel."
    data = _plaid_post("/accounts/balance/get", {"access_token": access_token})
    if "error" in data:
        return f"Couldn't fetch balance: {data['error'].get('error_message', 'unknown error')}"
    accounts = data.get("accounts", [])
    if not accounts:
        return "No accounts found."
    lines = []
    for a in accounts:
        name = a.get("name", "Account")
        bal  = a.get("balances", {})
        curr = bal.get("current", 0)
        avail = bal.get("available")
        atype = a.get("type", "")
        if avail is not None:
            lines.append(f"• {name} ({atype}): ${curr:,.2f} current / ${avail:,.2f} available")
        else:
            lines.append(f"• {name} ({atype}): ${curr:,.2f}")
    return "💳 Account Balances:\n" + "\n".join(lines)

def plaid_get_transactions_text(days=7):
    """Return a plain-text recent-transactions summary."""
    access_token = get_plaid_access_token()
    if not access_token:
        return "Bank not connected yet. Open Onyx and connect via the Finance panel."
    end_date   = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    data = _plaid_post("/transactions/get", {
        "access_token": access_token,
        "start_date": start_date,
        "end_date": end_date,
        "options": {"count": 20, "offset": 0},
    })
    if "error" in data:
        return f"Couldn't fetch transactions: {data['error'].get('error_message', 'unknown error')}"
    txns = data.get("transactions", [])
    if not txns:
        return f"No transactions found in the last {days} days."
    total_spent = sum(t["amount"] for t in txns if t["amount"] > 0)
    lines = [f"💸 Last {days} days — {len(txns)} transactions (${total_spent:,.2f} spent):"]
    for t in txns[:10]:
        sign  = "-" if t["amount"] < 0 else ""
        lines.append(f"  • {t['date']} {t['name'][:30]}: {sign}${abs(t['amount']):.2f}")
    return "\n".join(lines)

# ── Google Calendar helpers ──
def get_gcal_creds():
    token_json = _load_token("GCAL_TOKEN", "gcal_token.json")
    if not token_json:
        return None
    try:
        creds = Credentials.from_authorized_user_info(json.loads(token_json), GCAL_SCOPES)
    except Exception as e:
        print(f"[GCAL CREDS] parse error: {e}")
        return None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(GRequest())
            _save_token("GCAL_TOKEN", creds.to_json())
        except Exception as e:
            print(f"[GCAL CREDS] refresh error: {e}")
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

# ── Google Docs / Schedule helpers ──

def get_gdocs_service():
    creds = get_gcal_creds()
    if not creds:
        return None
    try:
        return build("docs", "v1", credentials=creds)
    except Exception as e:
        print(f"[DOCS] build error: {e}")
        return None

def _extract_gdoc_text(doc):
    """Walk Google Docs API response and return plain text."""
    parts = []
    def walk_content(content):
        for elem in content:
            if "paragraph" in elem:
                for pe in elem["paragraph"].get("elements", []):
                    if "textRun" in pe:
                        parts.append(pe["textRun"].get("content", ""))
            elif "table" in elem:
                for row in elem["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        walk_content(cell.get("content", []))
    walk_content(doc.get("body", {}).get("content", []))
    return "".join(parts)

def parse_shifts_with_ai(text):
    """Call Claude to extract shifts from doc text. Returns list of shift dicts."""
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = (
        f"Today is {today}. Extract all work shifts from the following schedule document.\n"
        "Return ONLY a JSON array (no markdown, no explanation) like:\n"
        '[{"date":"2026-03-16","day":"Monday","start":"09:00","end":"17:00","hours":8.0}]\n'
        "Rules: 24h time, dates as YYYY-MM-DD. Calculate hours from start/end if not listed.\n"
        "Only include shifts that appear to belong to Reed Ferguson (or if no names, include all).\n"
        "Return [] if no shifts found.\n\n"
        f"Document:\n{text[:6000]}"
    )
    result = ask_claude(prompt, max_tokens=1024)
    try:
        # Strip any accidental markdown fences
        clean = re.sub(r"```[a-z]*", "", result).strip()
        shifts = json.loads(clean)
        if isinstance(shifts, list):
            return shifts
    except Exception as e:
        print(f"[SCHEDULE] AI parse error: {e} | raw: {result[:200]}")
    return []

def add_shift_to_calendar(svc, shift):
    """Add a single shift to Google Calendar. Skip if an event already exists."""
    try:
        date_str  = shift["date"]        # YYYY-MM-DD
        start_str = shift["start"]       # HH:MM (24h)
        end_str   = shift["end"]         # HH:MM (24h)
        # Build RFC3339 datetimes (local timezone)
        import pytz
        tz = pytz.timezone(TIMEZONE)
        start_dt = tz.localize(datetime.strptime(f"{date_str} {start_str}", "%Y-%m-%d %H:%M"))
        end_dt   = tz.localize(datetime.strptime(f"{date_str} {end_str}",   "%Y-%m-%d %H:%M"))
        # Check for existing events on that day that look like a shift
        day_start = tz.localize(datetime.strptime(date_str, "%Y-%m-%d"))
        day_end   = day_start + timedelta(days=1)
        existing  = svc.events().list(
            calendarId="primary",
            timeMin=day_start.isoformat(),
            timeMax=day_end.isoformat(),
            q="Work",
            singleEvents=True,
        ).execute().get("items", [])
        for ev in existing:
            if ev.get("summary", "").startswith("Work"):
                print(f"[SCHEDULE] skipping duplicate on {date_str}")
                return False
        svc.events().insert(calendarId="primary", body={
            "summary": "Work — Donut Shop",
            "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
            "end":   {"dateTime": end_dt.isoformat(),   "timeZone": TIMEZONE},
            "description": f"Shift synced by Onyx | {shift.get('hours','')} hrs",
            "colorId": "6",  # tangerine
        }).execute()
        return True
    except Exception as e:
        print(f"[SCHEDULE] add_event error: {e}")
        return False

def fmt_time(t):
    """Convert 24h 'HH:MM' to 12h 'H:MM AM/PM'. Returns t unchanged if it can't be parsed."""
    try:
        return datetime.strptime(t, "%H:%M").strftime("%-I:%M %p")
    except Exception:
        return t

def paycheck_breakdown(gross):
    """Return dict with gross, deductions, and net for Indiana."""
    federal = round(gross * 0.12, 2)
    state   = round(gross * 0.0305, 2)
    fica    = round(gross * 0.0765, 2)
    total_deductions = round(federal + state + fica, 2)
    net = round(gross - total_deductions, 2)
    return {"gross": gross, "federal": federal, "state": state, "fica": fica,
            "total_deductions": total_deductions, "net": net}

def get_shifts_text():
    """Return upcoming shifts as plain text for WhatsApp/chat."""
    shifts = load_json("shifts.json", [])
    today  = datetime.now().strftime("%Y-%m-%d")
    upcoming = [s for s in shifts if s.get("date", "") >= today]
    if not upcoming:
        return "No upcoming shifts synced. Paste your schedule doc in the Schedule panel."
    lines = ["📅 Upcoming Shifts:"]
    total_hrs = 0.0
    for s in upcoming[:7]:
        hrs = s.get("hours", 0)
        total_hrs += float(hrs)
        lines.append(f"  • {s.get('day','')} {s['date']}  {fmt_time(s['start'])}–{fmt_time(s['end'])}  ({hrs}h)")
    if HOURLY_RATE > 0:
        b = paycheck_breakdown(round(total_hrs * HOURLY_RATE, 2))
        lines.append(f"\n💵 Gross: ${b['gross']:,.2f}  |  Taxes: -${b['total_deductions']:,.2f}  |  Take-home: ~${b['net']:,.2f}")
    return "\n".join(lines)

def get_next_shift_text():
    """Return just the next upcoming shift."""
    shifts = load_json("shifts.json", [])
    today  = datetime.now().strftime("%Y-%m-%d")
    upcoming = sorted([s for s in shifts if s.get("date","") >= today], key=lambda x: x["date"])
    if not upcoming:
        return "No upcoming shifts found."
    s = upcoming[0]
    hrs = s.get("hours", 0)
    msg = f"Your next shift is {s.get('day','')} {s['date']} from {fmt_time(s['start'])} to {fmt_time(s['end'])} ({hrs}h)"
    if HOURLY_RATE > 0:
        b = paycheck_breakdown(round(float(hrs) * HOURLY_RATE, 2))
        msg += f"  — Gross ${b['gross']:,.2f}, take-home ~${b['net']:,.2f} after taxes."
    return msg

def shift_reminder_check():
    """Run every 5 min — send WhatsApp if a shift starts in ~1 hour."""
    try:
        import pytz
        tz = pytz.timezone(TIMEZONE)
        now = datetime.now(tz)
        shifts = load_json("shifts.json", [])
        reminded = load_json("shifts_reminded.json", [])
        for s in shifts:
            key = f"{s['date']}-{s['start']}"
            if key in reminded:
                continue
            try:
                start_dt = tz.localize(datetime.strptime(f"{s['date']} {s['start']}", "%Y-%m-%d %H:%M"))
            except Exception:
                continue
            delta_min = (start_dt - now).total_seconds() / 60
            if 55 <= delta_min <= 65:
                msg = (f"⏰ Shift reminder: You work in 1 hour!\n"
                       f"📍 {s.get('day','')} {fmt_time(s['start'])}–{fmt_time(s['end'])} ({s.get('hours','')}h)")
                send_whatsapp(msg)
                reminded.append(key)
                save_json("shifts_reminded.json", reminded)
                print(f"[SCHEDULE] sent reminder for {key}")
    except Exception as e:
        print(f"[SCHEDULE] reminder error: {e}")

scheduler.add_job(shift_reminder_check, "interval", minutes=5, id="shift_reminder", replace_existing=True)

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


@app.route("/test-sms")
def test_sms():
    send_whatsapp("Reed AI Is Online And Running. 🤖")
    return jsonify({"sent": True})

@app.route("/agent/status")
def agent_status():
    return jsonify({"online": True, "scheduler": scheduler.state == STATE_RUNNING,
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

@app.route("/ping")
def ping():
    return jsonify({"status": "ok", "message": "Onyx is alive"})

# ── WhatsApp two-way chat ──
# Incoming messages from Twilio webhook are stored here
# Frontend polls /wa-messages and sends via /wa-send

@app.route("/wa-webhook", methods=["POST"])
def wa_webhook():
    """Receive incoming WhatsApp message from Twilio, store it, and auto-reply with AI."""
    import uuid as _uuid
    from_num = request.form.get("From", "")
    body     = request.form.get("Body", "").strip()
    # Always return empty TwiML immediately so Twilio doesn't retry
    twiml_ok = ('<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                200, {'Content-Type': 'text/xml'})
    if not body:
        return twiml_ok

    # 1. Store incoming message with dir='in'
    msgs = load_json("wa_inbox.json", [])
    msgs.append({
        "id":   str(_uuid.uuid4()),
        "from": from_num,
        "body": body,
        "dir":  "in",
        "ts":   int(datetime.now().timestamp() * 1000),
    })
    if len(msgs) > 500:
        msgs = msgs[-500:]
    save_json("wa_inbox.json", msgs)
    print(f"WA in from {from_num}: {body[:80]}")

    # 2. Generate AI reply in a background thread (don't block Twilio's 15s window)
    def _auto_reply(snapshot):
        try:
            reply_text = None

            # ── Path A: pending email clarification ──────────────────────────
            pending = wa_pending_emails.get(from_num)
            if pending:
                # Let Reed cancel mid-flow
                if re.match(r'^\s*(cancel|nevermind|never\s+mind|forget\s+it|stop|nope)\s*$',
                            body, re.IGNORECASE):
                    del wa_pending_emails[from_num]
                    reply_text = "Got it — email cancelled."

                elif pending["waiting_for"] == "email_address":
                    addr_match = re.search(r'[\w.+\-]+@[\w\-]+\.[a-zA-Z]{2,}', body)
                    if addr_match:
                        pending["to_email"] = addr_match.group()
                        if pending.get("body"):
                            # Have everything now — send
                            del wa_pending_emails[from_num]
                            ok, err = _wa_send_email(
                                pending["to_email"],
                                pending.get("subject", "Message from Reed"),
                                pending["body"],
                            )
                            reply_text = (f"Done — email sent to {pending['to_email']} ✓"
                                          if ok else f"Couldn't send the email: {err}")
                        else:
                            # Still need the body
                            pending["waiting_for"] = "message_body"
                            name = pending.get("to_name") or pending["to_email"]
                            reply_text = f"Got it. What do you want to say to {name}?"
                    else:
                        reply_text = ("That doesn't look like a valid email address. "
                                      "Try: name@example.com")

                elif pending["waiting_for"] == "message_body":
                    to_email = pending["to_email"]
                    subject  = pending.get("subject") or "Message from Reed"
                    del wa_pending_emails[from_num]
                    ok, err = _wa_send_email(to_email, subject, body)
                    label = pending.get("to_name") or to_email
                    reply_text = (f"Done — email sent to {label} ✓"
                                  if ok else f"Couldn't send the email: {err}")

            # ── Path B: fresh email intent ────────────────────────────────────
            if reply_text is None:
                intent = _wa_parse_email_intent(body)
                if intent:
                    to_email  = intent.get("to_email")
                    to_name   = intent.get("to_name") or to_email or "them"
                    subject   = intent.get("subject") or "Message from Reed"
                    body_text = intent.get("body")
                    missing   = intent.get("missing", "none")

                    if missing == "none" and to_email and body_text:
                        # Send immediately
                        ok, err = _wa_send_email(to_email, subject, body_text)
                        reply_text = (f"Done — email sent to {to_email} ✓"
                                      if ok else f"Couldn't send the email: {err}")

                    elif not to_email or missing in ("email_address", "both"):
                        # Need email address first
                        wa_pending_emails[from_num] = {
                            "to_name": to_name,
                            "to_email": to_email,
                            "subject":  subject,
                            "body":     body_text,
                            "waiting_for": "email_address",
                        }
                        reply_text = f"What's {to_name}'s email address?"

                    else:
                        # Have address but need the body
                        wa_pending_emails[from_num] = {
                            "to_name": to_name,
                            "to_email": to_email,
                            "subject":  subject,
                            "body":     None,
                            "waiting_for": "message_body",
                        }
                        reply_text = f"What do you want to say to {to_name}?"

            # ── Path B.5: on-demand daily briefing ───────────────────────────
            if reply_text is None:
                _BRIEFING_RE = re.compile(
                    r'^\s*('
                    r'good\s+morning|gm[!.]*|'
                    r'morning\s+briefing|daily\s+briefing|'
                    r'what.{0,10}my\s+day|'
                    r'day\s+look.{0,15}like|'
                    r'what.{0,10}today|'
                    r'briefing'
                    r')\s*[?!.]*\s*$',
                    re.IGNORECASE,
                )
                if _BRIEFING_RE.match(body):
                    reply_text = build_daily_briefing()

            # ── Path B.6: bank balance / transactions query ───────────────────
            if reply_text is None:
                _BANK_RE = re.compile(
                    r'\b(balance|how\s+much.*(?:have|left|in\s+(?:my|the))|'
                    r'what.{0,10}(?:account|bank|checking|saving)|'
                    r'recent\s+transactions?|transaction\s+history|'
                    r'how\s+much.*spent?|what.*(?:spend|spent)|spending\s+this\s+week|'
                    r'bank\s+balance|account\s+balance)\b',
                    re.IGNORECASE,
                )
                if _BANK_RE.search(body):
                    is_txn = re.search(r'\b(transaction|spent|spend|spending|history)\b', body, re.IGNORECASE)
                    reply_text = plaid_get_transactions_text(days=7) if is_txn else plaid_get_balance_text()

            # ── Path B.7: work schedule queries ──────────────────────────────
            if reply_text is None:
                _SCHED_RE = re.compile(
                    r'\b(when\s+do\s+i\s+work|next\s+shift|my\s+shift|work\s+(this\s+week|today|tomorrow|schedule)|'
                    r'how\s+many\s+hours|paycheck|pay\s*check|how\s+much.*pay|what.*i\s+make|'
                    r'shift\s+this\s+week|when\s+am\s+i\s+work)\b',
                    re.IGNORECASE,
                )
                if _SCHED_RE.search(body):
                    is_next = re.search(r'\b(next|today|tomorrow)\b', body, re.IGNORECASE)
                    is_pay  = re.search(r'\b(paycheck|pay\s*check|how\s+much.*pay|what.*make)\b', body, re.IGNORECASE)
                    if is_pay:
                        shifts = load_json("shifts.json", [])
                        today_str = datetime.now().strftime("%Y-%m-%d")
                        total_hrs = sum(float(s.get("hours",0)) for s in shifts if s.get("date","") >= today_str)
                        if HOURLY_RATE > 0 and total_hrs > 0:
                            b = paycheck_breakdown(round(total_hrs * HOURLY_RATE, 2))
                            reply_text = (f"💵 Paycheck ({total_hrs}h × ${HOURLY_RATE}/hr)\n"
                                          f"Gross: ${b['gross']:,.2f}\n"
                                          f"Federal (12%): -${b['federal']:,.2f}\n"
                                          f"Indiana (3.05%): -${b['state']:,.2f}\n"
                                          f"FICA (7.65%): -${b['fica']:,.2f}\n"
                                          f"Take-home: ~${b['net']:,.2f}")
                        else:
                            reply_text = "No shifts or hourly rate configured yet."
                    elif is_next:
                        reply_text = get_next_shift_text()
                    else:
                        reply_text = get_shifts_text()

            # ── Path B.8: light control ──────────────────────────────────────
            if reply_text is None:
                _LIGHT_RE = re.compile(
                    r'\b(turn\s+(my\s+)?lights?\s+(on|off)|'
                    r'lights?\s+(on|off)|'
                    r'set\s+(my\s+)?lights?\s+to\s+\w+|'
                    r'(set\s+)?(brightness|bright|dim)\s+(to\s+)?\d+|'
                    r'light\s+(color|colour)|'
                    r'make\s+(my\s+)?lights?\s+\w+)\b',
                    re.IGNORECASE,
                )
                if _LIGHT_RE.search(body):
                    reply_text = govee_wa_reply(body)

            # ── Path C: regular AI conversation ──────────────────────────────
            if reply_text is None:
                context = []
                for m in snapshot[-20:]:
                    role = "user" if m.get("dir") == "in" else "assistant"
                    if context and context[-1]["role"] == role:
                        context[-1]["content"] += "\n" + m["body"]
                    else:
                        context.append({"role": role, "content": m["body"]})
                while context and context[0]["role"] == "assistant":
                    context.pop(0)
                if not context:
                    context = [{"role": "user", "content": body}]

                r = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ANTHROPIC_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 350,
                        "system": (
                            "You are Onyx, Reed's personal AI assistant, responding via WhatsApp. "
                            "Reed Ferguson, Indianapolis, works at a donut shop, hunting for an office job "
                            "($18+/hr, no degree, 9-5 M-F), saving for a car, learning AI. "
                            "Be direct, conversational, and concise (1-3 sentences unless detail is needed). "
                            "Smart friend tone — no filler phrases, no 'certainly' or 'of course'."
                        ),
                        "messages": context,
                    },
                    timeout=25,
                )
                data = r.json()
                if data.get("error"):
                    print(f"WA AI error: {data['error']}")
                    return
                reply_text = "".join(
                    b["text"] for b in data.get("content", []) if b.get("type") == "text"
                ).strip()

            if not reply_text:
                return

            # ── Send reply via Twilio + store for Onyx UI ────────────────────
            to_wa = from_num if from_num.startswith("whatsapp:") else "whatsapp:" + from_num
            Client(TWILIO_SID, TWILIO_TOKEN).messages.create(
                body=reply_text,
                from_="whatsapp:" + TWILIO_FROM,
                to=to_wa,
            )
            all_msgs = load_json("wa_inbox.json", [])
            all_msgs.append({
                "id":   str(_uuid.uuid4()),
                "from": "whatsapp:" + TWILIO_FROM,
                "body": reply_text,
                "dir":  "out",
                "ts":   int(datetime.now().timestamp() * 1000),
            })
            if len(all_msgs) > 500:
                all_msgs = all_msgs[-500:]
            save_json("wa_inbox.json", all_msgs)
            print(f"WA reply sent: {reply_text[:80]}")
        except Exception as e:
            print(f"WA auto-reply error: {e}")

    threading.Thread(target=_auto_reply, args=(list(msgs),), daemon=True).start()
    return twiml_ok

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
    """Start OAuth flow — build auth URL manually, no PKCE."""
    import urllib.parse
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return jsonify({"error": "Google credentials not configured in Render env vars"}), 400
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(GCAL_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    auth_url = "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params)
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
    from datetime import timezone
    expiry = datetime.utcnow().replace(tzinfo=timezone.utc) + timedelta(seconds=int(token_data.get("expires_in", 3600)))
    creds = Credentials(
        token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=GCAL_SCOPES,
        expiry=expiry,
    )
    _save_token("GCAL_TOKEN", creds.to_json())
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
    _clear_token("GCAL_TOKEN", "gcal_token.json")
    return jsonify({"disconnected": True})

# ── Gmail helpers ──
def get_gmail_creds():
    token_json = _load_token("GMAIL_TOKEN", "gmail_token.json")
    if not token_json:
        return None
    try:
        creds = Credentials.from_authorized_user_info(json.loads(token_json), GMAIL_SCOPES)
    except Exception as e:
        print(f"[GMAIL CREDS] parse error: {e}")
        return None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(GRequest())
            _save_token("GMAIL_TOKEN", creds.to_json())
        except Exception as e:
            print(f"[GMAIL CREDS] refresh error: {e}")
            return None
    return creds if (creds and creds.valid) else None

def get_gmail_service():
    creds = get_gmail_creds()
    if not creds:
        return None
    return build("gmail", "v1", credentials=creds)

@app.route("/gmail/auth")
def gmail_auth():
    import urllib.parse
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return jsonify({"error": "Google credentials not configured"}), 400
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GMAIL_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(GMAIL_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    auth_url = "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params)
    return jsonify({"auth_url": auth_url})

@app.route("/gmail/callback")
def gmail_callback():
    code = request.args.get("code")
    if not code:
        return "<h2>Error: no code returned from Google</h2>", 400
    token_resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": GMAIL_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
    )
    token_data = token_resp.json()
    if "error" in token_data:
        return f"<h2>Token exchange error: {token_data.get('error_description', token_data.get('error'))}</h2>", 400
    from datetime import timezone
    expiry = datetime.utcnow().replace(tzinfo=timezone.utc) + timedelta(seconds=int(token_data.get("expires_in", 3600)))
    creds = Credentials(
        token=token_data["access_token"],
        refresh_token=token_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=GMAIL_SCOPES,
        expiry=expiry,
    )
    _save_token("GMAIL_TOKEN", creds.to_json())
    return """<html><head><style>body{font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;background:#0a0a0a;color:#fff;}
    .card{text-align:center;padding:40px;background:#1a1a1a;border-radius:16px;border:1px solid #333;}
    h2{color:#4ade80;margin:0 0 12px;}p{color:#999;margin:0;}</style></head>
    <body><div class="card"><h2>✓ Gmail Connected</h2><p>You can close this tab and return to Onyx.</p></div></body></html>"""

@app.route("/gmail/status")
def gmail_status():
    creds = get_gmail_creds()
    if not creds:
        return jsonify({"connected": False})
    try:
        svc = get_gmail_service()
        profile = svc.users().getProfile(userId="me").execute()
        return jsonify({"connected": True, "email": profile.get("emailAddress", "")})
    except Exception:
        return jsonify({"connected": True, "email": ""})

@app.route("/gmail/inbox")
def gmail_inbox():
    """Return recent inbox emails (read + unread)."""
    try:
        svc = get_gmail_service()
        if not svc:
            return jsonify({"error": "not_connected"}), 401
        max_results = int(request.args.get("max", 15))
        result = svc.users().messages().list(
            userId="me", labelIds=["INBOX"], maxResults=max_results
        ).execute()
        msgs = result.get("messages", [])
        emails = []
        for m in msgs:
            try:
                msg = svc.users().messages().get(
                    userId="me", id=m["id"], format="metadata",
                    metadataHeaders=["Subject", "From", "Date"]
                ).execute()
                headers = {h["name"]: h["value"]
                           for h in msg.get("payload", {}).get("headers", [])}
                emails.append({
                    "id": m["id"],
                    "threadId": msg.get("threadId", ""),
                    "subject": headers.get("Subject", "(no subject)"),
                    "from": headers.get("From", ""),
                    "date": headers.get("Date", ""),
                    "snippet": msg.get("snippet", ""),
                })
            except Exception as inner:
                print(f"[GMAIL INBOX] skipping message {m['id']}: {inner}")
                continue
        return jsonify({"emails": emails})
    except Exception as e:
        print(f"[GMAIL INBOX] error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/gmail/important")
def gmail_important():
    """Return important/starred emails or emails matching a keyword."""
    try:
        svc = get_gmail_service()
        if not svc:
            return jsonify({"error": "not_connected"}), 401
        keyword = request.args.get("q", "")
        max_results = int(request.args.get("max", 10))
        query = keyword if keyword else "is:important"
        result = svc.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()
        msgs = result.get("messages", [])
        emails = []
        for m in msgs:
            try:
                msg = svc.users().messages().get(
                    userId="me", id=m["id"], format="metadata",
                    metadataHeaders=["Subject", "From", "Date"]
                ).execute()
                headers = {h["name"]: h["value"]
                           for h in msg.get("payload", {}).get("headers", [])}
                emails.append({
                    "id": m["id"],
                    "threadId": msg.get("threadId", ""),
                    "subject": headers.get("Subject", "(no subject)"),
                    "from": headers.get("From", ""),
                    "date": headers.get("Date", ""),
                    "snippet": msg.get("snippet", ""),
                })
            except Exception as inner:
                print(f"[GMAIL IMPORTANT] skipping message {m['id']}: {inner}")
                continue
        return jsonify({"emails": emails})
    except Exception as e:
        print(f"[GMAIL IMPORTANT] error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/gmail/send", methods=["POST"])
def gmail_send():
    """Send an email. Body: {to, subject, body, threadId (optional for reply)}."""
    try:
        svc = get_gmail_service()
        if not svc:
            return jsonify({"error": "not_connected"}), 401
        data = request.get_json() or {}
        to = data.get("to", "")
        subject = data.get("subject", "")
        body = data.get("body", "")
        thread_id = data.get("threadId", "")
        if not to or not body:
            return jsonify({"error": "to and body are required"}), 400
        mime_msg = MIMEText(body)
        mime_msg["to"] = to
        mime_msg["subject"] = subject
        raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
        send_body = {"raw": raw}
        if thread_id:
            send_body["threadId"] = thread_id
        sent = svc.users().messages().send(userId="me", body=send_body).execute()
        return jsonify({"sent": True, "id": sent["id"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/gmail/disconnect", methods=["POST"])
def gmail_disconnect():
    _clear_token("GMAIL_TOKEN", "gmail_token.json")
    return jsonify({"disconnected": True})

# ── Plaid Routes ──

@app.route("/plaid/status")
def plaid_status():
    token = get_plaid_access_token()
    if not token:
        return jsonify({"connected": False})
    data = _plaid_post("/accounts/balance/get", {"access_token": token})
    if "error" in data:
        return jsonify({"connected": False})
    accounts = data.get("accounts", [])
    names = [a.get("name", "") for a in accounts]
    return jsonify({"connected": True, "accounts": names})

@app.route("/plaid/link", methods=["POST"])
def plaid_link():
    if not PLAID_CLIENT_ID or not PLAID_SECRET:
        return jsonify({"error": "PLAID_CLIENT_ID and PLAID_SECRET not configured in Render env vars"}), 400
    data = _plaid_post("/link/token/create", {
        "user": {
            "client_user_id": "reed",
            "phone_number": "+13175550000",
            "phone_number_verified_time": "2024-01-01T00:00:00Z",
        },
        "client_name": "Onyx",
        "products": ["transactions"],
        "country_codes": ["US"],
        "language": "en",
    })
    if "error" in data:
        return jsonify({"error": data["error"].get("error_message", "Plaid error")}), 400
    return jsonify({"link_token": data["link_token"]})

@app.route("/plaid/exchange", methods=["POST"])
def plaid_exchange():
    body = request.get_json() or {}
    public_token = body.get("public_token", "")
    if not public_token:
        return jsonify({"error": "No public_token"}), 400
    data = _plaid_post("/item/public_token/exchange", {"public_token": public_token})
    if "error" in data:
        return jsonify({"error": data["error"].get("error_message", "Exchange failed")}), 400
    save_plaid_access_token(data["access_token"])
    return jsonify({"success": True})

@app.route("/plaid/balance")
def plaid_balance():
    access_token = get_plaid_access_token()
    if not access_token:
        return jsonify({"error": "not_connected"}), 401
    data = _plaid_post("/accounts/balance/get", {"access_token": access_token})
    if "error" in data:
        return jsonify({"error": data["error"].get("error_message", "Plaid error")}), 400
    accounts = []
    for a in data.get("accounts", []):
        bal = a.get("balances", {})
        accounts.append({
            "account_id": a.get("account_id", ""),
            "name":       a.get("name", "Account"),
            "type":       a.get("type", ""),
            "subtype":    a.get("subtype", ""),
            "current":    bal.get("current"),
            "available":  bal.get("available"),
            "iso_currency_code": bal.get("iso_currency_code", "USD"),
        })
    return jsonify({"accounts": accounts})

@app.route("/plaid/transactions")
def plaid_transactions():
    access_token = get_plaid_access_token()
    if not access_token:
        return jsonify({"error": "not_connected"}), 401
    days = int(request.args.get("days", 30))
    end_date   = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    data = _plaid_post("/transactions/get", {
        "access_token": access_token,
        "start_date": start_date,
        "end_date": end_date,
        "options": {"count": 50, "offset": 0},
    })
    if "error" in data:
        return jsonify({"error": data["error"].get("error_message", "Plaid error")}), 400
    txns = []
    for t in data.get("transactions", []):
        txns.append({
            "transaction_id": t.get("transaction_id", ""),
            "date":           t.get("date", ""),
            "name":           t.get("name", ""),
            "amount":         t.get("amount", 0),
            "category":       t.get("category", []),
            "account_id":     t.get("account_id", ""),
        })
    return jsonify({"transactions": txns, "total_transactions": data.get("total_transactions", 0)})

@app.route("/plaid/disconnect", methods=["POST"])
def plaid_disconnect():
    os.environ.pop("PLAID_ACCESS_TOKEN", None)
    try:
        os.remove("plaid_token.txt")
    except Exception:
        pass
    return jsonify({"disconnected": True})

# ── Govee Routes ──

@app.route("/govee/devices")
def govee_devices():
    if not GOVEE_API_KEY:
        return jsonify({"error": "GOVEE_API_KEY not configured"}), 503
    devices = govee_get_devices()
    return jsonify({"devices": devices})

@app.route("/govee/control", methods=["POST"])
def govee_control():
    if not GOVEE_API_KEY:
        return jsonify({"error": "GOVEE_API_KEY not configured"}), 503
    data = request.get_json() or {}
    cmd_name  = data.get("cmd")    # "turn" | "color" | "brightness"
    cmd_value = data.get("value")  # "on"/"off" | {r,g,b} | 0-100
    device    = data.get("device") # specific device address, or None = all
    model     = data.get("model")  # required if device is specified

    if not cmd_name or cmd_value is None:
        return jsonify({"error": "cmd and value required"}), 400

    if device and model:
        # Single device
        try:
            payload = {"device": device, "model": model,
                       "cmd": {"name": cmd_name, "value": cmd_value}}
            r = requests.put(f"{GOVEE_BASE}/control", headers=_govee_headers(),
                             json=payload, timeout=10)
            return jsonify(r.json()), r.status_code
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    else:
        ok, detail = govee_control_all(cmd_name, cmd_value)
        return jsonify({"ok": ok, "detail": detail})

# ── Voice Routes ──

@app.route("/voice/transcribe", methods=["POST"])
def voice_transcribe():
    if not OPENAI_API_KEY:
        return jsonify({"error": "OPENAI_API_KEY not configured"}), 503
    if "audio" not in request.files:
        return jsonify({"error": "audio file required"}), 400
    audio_file = request.files["audio"]
    try:
        r = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={"file": (audio_file.filename or "audio.webm",
                            audio_file.stream,
                            audio_file.content_type or "audio/webm")},
            data={"model": "whisper-1", **( {"prompt": request.form.get("prompt")} if request.form.get("prompt") else {} )},
            timeout=30,
        )
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/voice/tts", methods=["POST"])
def voice_tts():
    if not OPENAI_API_KEY:
        return jsonify({"error": "OPENAI_API_KEY not configured"}), 503
    data = request.get_json() or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    # Strip markdown so it's not read literally
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'#+\s*', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = text[:4096]  # OpenAI TTS max
    try:
        from flask import Response as FlaskResponse
        r = requests.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": "tts-1", "input": text, "voice": "onyx"},
            timeout=30,
        )
        if r.status_code != 200:
            return jsonify({"error": r.text[:200]}), r.status_code
        return FlaskResponse(r.content, content_type="audio/mpeg")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Schedule Routes ──

@app.route("/schedule/sync", methods=["POST"])
def schedule_sync():
    import concurrent.futures, time
    t0 = time.time()
    def elapsed(): return f"{time.time()-t0:.1f}s"

    body = request.get_json() or {}
    doc_url = body.get("doc_url", "").strip()
    if not doc_url:
        return jsonify({"error": "doc_url required"}), 400

    m = re.search(r"/document(?:/u/\d+)?/d/([a-zA-Z0-9_-]+)", doc_url)
    if not m:
        return jsonify({"error": "Could not find a Google Doc ID in that URL"}), 400
    doc_id = m.group(1)
    print(f"[SCHEDULE SYNC] start — doc_id={doc_id}")

    # ── Step 1: fetch Google Doc ──────────────────────────────────────────────
    print(f"[SCHEDULE SYNC] step 1: fetching doc ({elapsed()})")
    svc = get_gdocs_service()
    if not svc:
        print(f"[SCHEDULE SYNC] step 1 FAIL: no Google Docs service — token missing or lacks Docs scope")
        return jsonify({"error": "Google not authorized. Re-authorize Google Calendar in Onyx to grant Docs access."}), 401
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(svc.documents().get(documentId=doc_id).execute)
            doc = future.result(timeout=10)
    except concurrent.futures.TimeoutError:
        print(f"[SCHEDULE SYNC] step 1 TIMEOUT after 10s fetching doc")
        return jsonify({"error": "Timed out fetching the Google Doc (>10s). Check that the doc is shared/accessible."}), 504
    except Exception as e:
        err_str = str(e)
        print(f"[SCHEDULE SYNC] step 1 ERROR: {err_str}")
        if "insufficientPermissions" in err_str or "403" in err_str:
            return jsonify({"error": "Docs scope not granted. Re-authorize Google Calendar in Onyx → Settings."}), 403
        if "404" in err_str:
            return jsonify({"error": "Doc not found — make sure the link is correct and the doc is shared."}), 404
        return jsonify({"error": f"Could not read doc: {err_str[:150]}"}), 400

    text = _extract_gdoc_text(doc)
    print(f"[SCHEDULE SYNC] step 1 OK: extracted {len(text)} chars ({elapsed()})")
    if not text.strip():
        return jsonify({"error": "Doc appears empty or has no readable text."}), 400

    # ── Step 2: parse shifts with AI ─────────────────────────────────────────
    print(f"[SCHEDULE SYNC] step 2: parsing shifts with AI ({elapsed()})")
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(parse_shifts_with_ai, text)
            shifts = future.result(timeout=20)
    except concurrent.futures.TimeoutError:
        print(f"[SCHEDULE SYNC] step 2 TIMEOUT after 20s in AI parse")
        return jsonify({"error": "AI timed out parsing the schedule (>20s). Try again."}), 504
    except Exception as e:
        print(f"[SCHEDULE SYNC] step 2 ERROR: {e}")
        return jsonify({"error": f"AI parse failed: {str(e)[:150]}"}), 500

    print(f"[SCHEDULE SYNC] step 2 OK: found {len(shifts)} shifts ({elapsed()})")
    if not shifts:
        return jsonify({"error": "No shifts found in document. Make sure it's the correct weekly schedule."}), 400

    save_json("shifts.json", shifts)
    save_json("shifts_reminded.json", [])

    # ── Step 3: add to Google Calendar ───────────────────────────────────────
    print(f"[SCHEDULE SYNC] step 3: adding {len(shifts)} shifts to Google Calendar ({elapsed()})")
    added = 0
    cal_svc = get_gcal_service()
    if not cal_svc:
        print(f"[SCHEDULE SYNC] step 3 SKIP: no Calendar service")
    else:
        for s in shifts:
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    future = ex.submit(add_shift_to_calendar, cal_svc, s)
                    if future.result(timeout=8):
                        added += 1
            except concurrent.futures.TimeoutError:
                print(f"[SCHEDULE SYNC] step 3 timeout on shift {s.get('date')} — skipping")
            except Exception as e:
                print(f"[SCHEDULE SYNC] step 3 error on shift {s.get('date')}: {e}")

    print(f"[SCHEDULE SYNC] step 3 OK: added {added} calendar events ({elapsed()})")

    today = datetime.now().strftime("%Y-%m-%d")
    total_hrs = sum(float(s.get("hours", 0)) for s in shifts if s.get("date", "") >= today)
    gross = round(total_hrs * HOURLY_RATE, 2) if HOURLY_RATE > 0 else None
    pay = paycheck_breakdown(gross) if gross is not None else None
    print(f"[SCHEDULE SYNC] done — {len(shifts)} shifts, {added} cal events, {total_hrs}h, ${gross} ({elapsed()})")
    return jsonify({"shifts": shifts, "calendar_events_added": added, "total_hours": total_hrs, "paycheck": pay})

@app.route("/schedule/shifts")
def schedule_shifts():
    shifts = load_json("shifts.json", [])
    today  = datetime.now().strftime("%Y-%m-%d")
    total_hrs = sum(float(s.get("hours", 0)) for s in shifts if s.get("date","") >= today)
    gross = round(total_hrs * HOURLY_RATE, 2) if HOURLY_RATE > 0 else None
    pay = paycheck_breakdown(gross) if gross is not None else None
    return jsonify({"shifts": shifts, "total_hours": total_hrs, "paycheck": pay, "hourly_rate": HOURLY_RATE})

@app.route("/schedule/shifts/clear", methods=["POST"])
def schedule_shifts_clear():
    save_json("shifts.json", [])
    save_json("shifts_reminded.json", [])
    return jsonify({"cleared": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

