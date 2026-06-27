#!/usr/bin/env python3
"""
Appointment Sentinel
Monitors HotDoc for the next available appointment and emails you
when an earlier slot becomes available than the one previously recorded.

Designed to run as a GitHub Actions scheduled job.
State (the WORST date) is persisted in state/worst.json, which is committed
back to the repository after each run by the workflow.
"""

import json
import os
import re
import smtplib
import logging
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ─── CONFIGURATION ────────────────────────────────────────────────────────────

HOTDOC_URL = (
    "https://www.hotdoc.com.au/medical-centres/blackbutt-QLD-4306/blackbutt-medical-centre/doctors/lorna-montgomery"
)

# Email – set via GitHub Actions secrets (see README)
SMTP_HOST     = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
ALERT_TO      = os.getenv("ALERT_TO", "")

# Persisted state file (committed back to repo by the workflow)
STATE_FILE = Path("worst.json")

# ─── LOGGING ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ─── STATE ────────────────────────────────────────────────────────────────────

def load_worst() -> datetime | None:
    if STATE_FILE.exists():
        try:
            raw = json.loads(STATE_FILE.read_text()).get("worst")
            return datetime.fromisoformat(raw) if raw else None
        except Exception as exc:
            log.warning("Could not read state: %s", exc)
    return None


def save_worst(dt: datetime) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"worst": dt.isoformat()}, indent=2))
    log.info("WORST date saved → %s", dt.date())

# ─── SCRAPING ─────────────────────────────────────────────────────────────────

def get_next_appointment() -> datetime | None:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    driver = webdriver.Chrome(options=opts)
    try:
        log.info("Fetching HotDoc page …")
        driver.get(HOTDOC_URL)

        wait = WebDriverWait(driver, 20)
        slots = wait.until(
            EC.presence_of_all_elements_located(
                (
                    By.CSS_SELECTOR,
                    "[data-testid='appointment-slot'], "
                    ".appointment-slot, "
                    "[class*='AppointmentSlot'], "
                    "[class*='available-slot'], "
                    "button[aria-label*='appointment']",
                )
            )
        )
        log.info("Found %d slot element(s).", len(slots))

        dates: list[datetime] = []
        for slot in slots:
            for attr in ("datetime", "data-datetime", "aria-label", "title"):
                raw = slot.get_attribute(attr) or ""
                dt = _try_parse(raw.strip())
                if dt:
                    dates.append(dt)
                    break

        if not dates:
            log.info("No dated slots via CSS; scanning page text …")
            dates = _extract_dates_from_text(
                driver.find_element(By.TAG_NAME, "body").text
            )

        if dates:
            earliest = min(dates)
            log.info("Earliest slot found: %s", earliest)
            return earliest

        log.info("No appointment dates found.")
        return None

    except Exception as exc:
        log.error("Scrape error: %s", exc)
        return None
    finally:
        driver.quit()


_DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%d %b %Y %I:%M %p",
    "%A %d %B %Y",
]


def _try_parse(text: str) -> datetime | None:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _extract_dates_from_text(text: str) -> list[datetime]:
    pattern = re.compile(
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2} \w+ \d{4})"
        r"(?:[^\d]*(\d{1,2}:\d{2}(?:\s?[APap][Mm])?))?",
        re.IGNORECASE,
    )
    results = []
    for m in pattern.finditer(text):
        dt = _try_parse(m.group(0).strip())
        if dt:
            results.append(dt)
    return results

# ─── EMAIL ────────────────────────────────────────────────────────────────────

def send_alert(new_date: datetime, worst_date: datetime) -> None:
    if not all([SMTP_USER, SMTP_PASSWORD, ALERT_TO]):
        log.warning("Email credentials not configured – skipping alert.")
        return

    subject = f"🗓 Earlier appointment available – {new_date.strftime('%a %-d %b %Y')}"
    body = (
        f"An earlier appointment with Dr Lorna Montgomery is now available!\n\n"
        f"  New slot  : {new_date.strftime('%A %-d %B %Y')}\n"
        f"  Previous  : {worst_date.strftime('%A %-d %B %Y')}\n\n"
        f"Book now → {HOTDOC_URL}\n\n"
        f"— Appointment Sentinel"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = ALERT_TO
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, ALERT_TO, msg.as_string())
        log.info("Alert sent to %s ✓", ALERT_TO)
    except Exception as exc:
        log.error("Failed to send email: %s", exc)

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=== Appointment Sentinel ===")
    worst = load_worst()
    log.info("Loaded WORST: %s", worst.date() if worst else "None")

    next_appt = get_next_appointment()

    if next_appt is None:
        log.info("No appointment found this run.")
        return

    if worst is None:
        # First ever run – record this date as WORST and start watching.
        log.info("First run – recording WORST as %s", next_appt.date())
        save_worst(next_appt)
        return

    if next_appt >= worst:
        # Slot is the same or later – update WORST if later, then keep watching.
        if next_appt > worst:
            log.info("Slot moved later (%s → %s) – updating WORST.", worst.date(), next_appt.date())
            save_worst(next_appt)
        else:
            log.info("No change (%s = WORST) – nothing to do.", next_appt.date())
        return

    # next_appt < worst → strictly earlier slot found!
    log.info("🎉 Earlier slot found: %s < WORST %s", next_appt.date(), worst.date())
    send_alert(next_appt, worst)
    # Note: WORST is intentionally NOT updated here.
    # This means we keep alerting on subsequent runs as long as any slot
    # remains earlier than the original WORST — including Aug 1 < Aug 3, etc.


if __name__ == "__main__":
    main()
