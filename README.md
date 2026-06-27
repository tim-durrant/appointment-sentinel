# Appointment Sentinel 🗓

Monitors HotDoc every 30 minutes via **GitHub Actions** and emails you when an
earlier appointment becomes available with Dr Lorna Montgomery at Blackbutt Medical Centre.

---

## How it works

| Step | Logic |
|------|-------|
| 1 | GitHub Actions runs `sentinel.py` on a cron every 30 minutes |
| 2 | Headless Chrome scrapes the next available appointment date from HotDoc |
| 3 | If no WORST date recorded yet → save this date as WORST, done |
| 4 | If new date > WORST → slot moved later → update WORST, done |
| 5 | If new date ≤ WORST → **earlier slot found!** → send email alert 🎉 |

The WORST date is stored in `state/worst.json` and automatically committed back
to the repo after each run so state persists between Actions jobs.

---

## Setup (one-time)

### 1 — Create a new GitHub repository

```bash
# Download/clone this folder from Google Drive, then:
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
| `ALERT_TO`      | Where to send alerts (can be same as `SMTP_USER`) |

> **Gmail App Password**: Go to Google Account → Security → 2-Step Verification → App Passwords.
> Generate one for "Mail" and use it here. Do **not** use your normal Gmail password.

### 3 — Allow Actions to push commits

Go to **Settings → Actions → General → Workflow permissions** and select
**"Read and write permissions"**.

### 4 — Trigger manually to test

Go to **Actions → Appointment Sentinel → Run workflow** to run it immediately
without waiting for the cron.

---

## File structure

```
appointment-sentinel/
├── .github/
│   └── workflows/
│       └── sentinel.yml      ← GitHub Actions workflow (runs every 30 min)
├── state/
│   └── worst.json            ← Persisted WORST date (auto-updated by Actions)
├── sentinel.py               ← Main script
├── requirements.txt          ← Python dependencies
├── .gitignore
└── README.md
```

---

## Customisation

- **Check interval**: Edit the `cron` expression in `.github/workflows/sentinel.yml`
- **Doctor URL**: Change `HOTDOC_URL` at the top of `sentinel.py`
- **Selector tuning**: If HotDoc updates its markup, adjust the CSS selector in
  `get_next_appointment()` inside `sentinel.py`
