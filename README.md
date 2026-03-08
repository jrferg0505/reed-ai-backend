# Reed AI Backend

Runs 24/7 on Render. Sends morning briefings, job alerts, and weekly recaps via SMS.

## Deploy To Render (Step By Step)

### 1. Upload To GitHub
- Go to github.com → New Repository → Name it `reed-ai-backend` → Create
- Upload all 4 files: main.py, requirements.txt, Procfile, README.md

### 2. Create Web Service On Render
- Go to render.com → New → Web Service
- Connect your GitHub account → Select `reed-ai-backend`
- Settings:
  - **Name:** reed-ai-backend
  - **Runtime:** Python 3
  - **Build Command:** `pip install -r requirements.txt`
  - **Start Command:** `gunicorn main:app --bind 0.0.0.0:$PORT --workers 1 --threads 2`
  - **Instance Type:** Free

### 3. Add Environment Variables
In Render → Your Service → Environment → Add these exactly:

| Key | Value |
|-----|-------|
| ANTHROPIC_KEY | your-anthropic-api-key |
| TWILIO_SID | your-twilio-account-sid |
| TWILIO_TOKEN | your-twilio-auth-token |
| TWILIO_FROM | your-twilio-phone-number |
| REED_PHONE | your-real-phone-number |
| BRIEFING_HOUR | 8 |
| TIMEZONE | America/New_York |

### 4. Deploy
- Click "Create Web Service"
- Wait ~2 minutes for it to build
- Once it shows "Live" go to: `https://your-service-name.onrender.com/test-sms`
- You should get a text within 30 seconds

## Test Endpoints
- `/test-sms` — sends a test text to confirm everything works
- `/run-briefing` — triggers a morning briefing right now
- `/run-jobs` — triggers a job scan right now
- `/ping` — confirms server is alive

## Schedule
- **8:00 AM EST daily** — Morning briefing
- **Every 6 hours** — Job scan (only texts if new jobs found)
- **Sunday 6:00 PM EST** — Weekly recap
