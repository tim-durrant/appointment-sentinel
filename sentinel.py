#!/usr/bin/env python3
"""
Appointment Sentinel
Monitors HotDoc for the next available appointment and emails you
when an earlier slot becomes available than the one previously recorded.

Email deduplication rules:
  - Only send if "new slot" or "previous" has changed since the last email, OR
  - 24 hours have passed since the last email with the same content.
"""

import json
import os
import re
import smtplib
import logging
import sys
import traceback
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException

# ─── CONFIGURATION ────────────────────────────────────────────────────────────

HOTDOC_URL = (
    "https://www.hotdoc.com.au/medical-centres/blackbutt-QLD-4306/"
    "blackbutt-medical-centre/doctors/lorna-montgomery"
)

AVAILABILITY_LABEL  = "Appointments available from:"
PAGE_LOAD_TIMEOUT   = 30
EMAIL_REPEAT_HOURS  = 24   # Re-send same content after this many hours

SMTP_HOST     = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
ALERT_TO      = os.getenv("ALERT_TO", "")

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
#
# State file schema:
# {
#   "worst": "2026-08-03T14:30:00",   ← latest known appointment datetime
#   "last_email": {
#     "new_slot":  "2026-07-15T09:00:00",
#     "previous":  "2026-08-03T14:30:00",
#     "sent_at":   "2026-06-27T08:00:00"
#   }
# }

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception as exc:
            log.warning("Could not read state: %s", exc)
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def load_worst() -> datetime | None:
    raw = _load_state().get("worst")
    return datetime.fromisoformat(raw) if raw else None


def save_worst(dt: datetime) -> None:
    state = _load_state()
    state["worst"] = dt.isoformat()
    _save_state(state)
    log.info("WORST date saved → %s", dt)


def load_last_email() -> dict | None:
    return _load_state().get("last_email")


def save_last_email(new_slot: datetime, previous: datetime) -> None:
    state = _load_state()
    state["last_email"] = {
        "new_slot":  new_slot.isoformat(),
        "previous":  previous.isoformat(),
        "sent_at":   datetime.now().isoformat(),
    }
    _save_state(state)
    log.info("Last email record saved.")

# ─── EMAIL DEDUPLICATION ──────────────────────────────────────────────────────

def should_send_email(new_slot: datetime, previous: datetime) -> bool:
    """
    Return True if we should send an alert email, based on:
      1. Content changed (new_slot or previous differs from last email), OR
      2. 24+ hours have passed since the last email with identical content.
    """
    last = load_last_email()

    if last is None:
        log.info("No previous email on record – will send.")
        return True

    last_new_slot  = datetime.fromisoformat(last["new_slot"])
    last_previous  = datetime.fromisoformat(last["previous"])
    last_sent_at   = datetime.fromisoformat(last["sent_at"])

    content_changed = (new_slot != last_new_slot) or (previous != last_previous)
    if content_changed:
        log.info("Email content has changed – will send.")
        return True

    hours_since = (datetime.now() - last_sent_at).total_seconds() / 3600
    if hours_since >= EMAIL_REPEAT_HOURS:
        log.info("%.1f hours since last identical email – will resend.", hours_since)
        return True

    log.info(
        "Suppressing duplicate email (content unchanged, only %.1fh since last send).",
        hours_since,
    )
    return False

# ─── SCRAPING ─────────────────────────────────────────────────────────────────

def _make_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    return webdriver.Chrome(options=opts)


def get_next_appointment() -> datetime | None:
    driver = _make_driver()
    try:
        log.info("Navigating to HotDoc page …")
        driver.get(HOTDOC_URL)

        log.info("Waiting for '%s' to appear …", AVAILABILITY_LABEL)
        try:
            WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(
                EC.text_to_be_present_in_element(
                    (By.TAG_NAME, "body"), AVAILABILITY_LABEL
                )
            )
        except TimeoutException:
            log.warning("Timed out waiting for availability text.")
            driver.save_screenshot("debug_screenshot.png")
            try:
                log.info("Page text:\n%s", driver.find_element(By.TAG_NAME, "body").text[:800])
            except Exception:
                pass
            return None

        page_text = driver.find_element(By.TAG_NAME, "body").text
        idx = page_text.find(AVAILABILITY_LABEL)
        if idx == -1:
            log.error("Label not found in page text.")
            return None

        after_label = page_text[idx + len(AVAILABILITY_LABEL):].strip()[:40]
        log.info("Text after label: '%s'", after_label)

        dt = _parse_hotdoc_date(after_label)
        if dt:
            log.info("Parsed appointment date: %s", dt)
        else:
            log.warning("Could not parse date from: '%s'", after_label)
        return dt

    except Exception as exc:
        log.error("Scrape error: %s", exc)
        log.error(traceback.format_exc())
        try:
            driver.save_screenshot("debug_screenshot.png")
        except Exception:
            pass
        return None
    finally:
        driver.quit()


def _parse_hotdoc_date(text: str) -> datetime | None:
    text = text.split("\n")[0].strip().rstrip(".")
    pattern = re.compile(
        r"(\d{1,2})\s+(\w+)"
        r"(?:,\s*(\d{1,2}:\d{2})\s*([aApP][mM]))?",
        re.IGNORECASE,
    )
    m = pattern.search(text)
    if not m:
        return None

    day, month, ttime, ampm = m.group(1), m.group(2), m.group(3), m.group(4)
    year = datetime.now().year
    try:
        if datetime.strptime(f"{day} {month} {year}", "%d %b %Y") < datetime.now():
            year += 1
    except ValueError:
        pass

    if ttime and ampm:
        date_str = f"{day} {month} {year} {ttime} {ampm.upper()}"
        fmt = "%d %b %Y %I:%M %p"
    else:
        date_str = f"{day} {month} {year}"
        fmt = "%d %b %Y"

    try:
        return datetime.strptime(date_str, fmt)
    except ValueError as exc:
        log.warning("strptime failed for '%s': %s", date_str, exc)
        return None

# ─── EMAIL ────────────────────────────────────────────────────────────────────

def send_alert(new_date: datetime, worst_date: datetime) -> None:
    if not all([SMTP_USER, SMTP_PASSWORD, ALERT_TO]):
        log.warning("Email credentials not configured – skipping alert.")
        return

    if not should_send_email(new_date, worst_date):
        return

    def fmt(dt: datetime) -> str:
        return dt.strftime("%-d %b %Y at %-I:%M %p") if dt.hour or dt.minute else dt.strftime("%-d %b %Y")

    subject = f"🗓 Earlier appointment available – {fmt(new_date)}"
    body = (
        f"An earlier appointment with Dr Lorna Montgomery is now available!\n\n"
        f"  New slot  : {fmt(new_date)}\n"
        f"  Previous  : {fmt(worst_date)}\n\n"
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
        save_last_email(new_date, worst_date)
    except Exception as exc:
        log.error("Failed to send email: %s", exc)

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=== Appointment Sentinel ===")
    worst = load_worst()
    log.info("Loaded WORST: %s", worst if worst else "None")

    next_appt = get_next_appointment()

    if next_appt is None:
        log.info("No appointment found this run.")
        return

    if worst is None:
        log.info("First run – recording WORST as %s", next_appt)
        save_worst(next_appt)
        return

    if next_appt >= worst:
        if next_appt > worst:
            log.info("Slot moved later (%s → %s) – updating WORST.", worst, next_appt)
            save_worst(next_appt)
        else:
            log.info("No change – nothing to do.")
        return

    # next_appt < worst → earlier slot found!
    log.info("🎉 Earlier slot found: %s < WORST %s", next_appt, worst)
    send_alert(next_appt, worst)
    # WORST intentionally NOT updated – keeps alerting until you book.


if __name__ == "__main__":
    main()
