# Appointment Sentinel 🗓

Monitors HotDoc every 30 minutes via **GitHub Actions** and emails you when an
earlier appointment becomes available .

---

## How it works

| Step | Logic |
|------|-------|
| 1 | GitHub Actions runs `sentinel.py` on a cron every 30 minutes |
| 2 | Headless Chrome opens the HotDoc page and waits for the text **"Appointments available from:"** to appear |
| 3 | The date immediately following that label is extracted and parsed |
| 4 | If no WORST date recorded yet → save this date as WORST, done |
| 5 | If new date > WORST → slot moved later → update WORST, done |
| 6 | If new date < WORST → **earlier slot found!** → send email alert 🎉 |

### Email deduplication

To avoid inbox spam, the script tracks the content and time of the last email sent:

| Situation | Result |
|-----------|--------|
| No email ever sent | Send ✓ |
| New slot date has changed | Send ✓ |
| Previous (WORST) date has changed | Send ✓ |
| Same content, sent less than 24 hours ago | Suppress ✗ |
| Same content, sent 24+ hours ago | Re-send as a reminder ✓ |

### State persistence

All state is stored in `worst.json` in the root of the repository and automatically
committed back after each run by the workflow. This file tracks:

```json
{
  "worst": "2026-08-03T14:30:00",
  "last_email": {
    "new_slot":  "2026-07-15T09:00:00",
    "previous":  "2026-08-03T14:30:00",
    "sent_at":   "2026-06-27T08:00:00"
  }
}
```

---

## Setup (one-time)

### 1 — Create a new GitHub repository

Download this folder from Google Drive, then run:

```bash
git init
git add .
git commit -m "Initial commit"
gh repo create appointment-sentinel --private --source=. --push
```

### 2 — Add GitHub Actions Secrets

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**

| Secret name     | Value |
|-----------------|-------|
| `SMTP_HOST`     | `smtp.gmail.com` |
| `SMTP_PORT`     | `587` |
| `SMTP_USER`     | Your Gmail address |
| `SMTP_PASSWORD` | A [Gmail App Password](https://myaccount.google.com/apppasswords) |
| `ALERT_TO`      | Where to send alerts (can be the same as `SMTP_USER`) |

> **Gmail App Password:** Go to Google Account → Security → 2-Step Verification → App Passwords.
> Generate one for "Mail" and paste it here. Do **not** use your normal Gmail password.

### 3 — Allow Actions to push commits

Go to **Settings → Actions → General → Workflow permissions** and select
**"Read and write permissions"**.

### 4 — Trigger manually to test

Go to **Actions → Appointment Sentinel → Run workflow** to run immediately
without waiting for the cron. After a successful run you should see a commit
from `github-actions[bot]` updating `worst.json` — that confirms state is
being persisted correctly.

---

## File structure

```
appointment-sentinel/
├── .github/
│   └── workflows/
│       └── sentinel.yml      ← GitHub Actions workflow (runs every 30 min)
├── sentinel.py               ← Main script
├── requirements.txt          ← Python dependencies (selenium)
├── worst.json                ← Persisted state (auto-committed by Actions)
├── .gitignore
└── README.md
```

---

## Customisation

- **Check interval:** Edit the `cron` expression in `.github/workflows/sentinel.yml`
- **Doctor URL:** Change `HOTDOC_URL` at the top of `sentinel.py`
- **Email repeat window:** Change `EMAIL_REPEAT_HOURS` in `sentinel.py` (default: 24)
- **Email display name:** The From field shows as `Appointment Sentinel <your@gmail.com>`.
  To change the display name, edit the `msg["From"]` line in `send_alert()`.

---

## Debugging

If a run fails, the workflow uploads a `debug_screenshot.png` as a downloadable
artifact so you can see exactly what the headless browser rendered. Find it under:

**Actions → (failed run) → Artifacts → debug-screenshot**
