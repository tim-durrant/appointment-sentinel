#!/usr/bin/env python3
"""
Appointment Sentinel
Monitors HotDoc for the next available appointment and emails you
when an earlier slot becomes available than the one previously recorded.

Designed to run as a GitHub Actions scheduled job.
State (the WORST date) is persisted in state/worst.json, which is committed
back to the repository after each run by the workflow.

Scraping strategy:
  Wait for the text "Appointments available from:" to appear on the page,
  then extract the date/time that follows it.
"""

import json
import os
import re
import smtplib
import logging
import sys
import traceback
import time
from datetime import datetime
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

# The label text that appears just before the date we want
AVAILABILITY_LABEL = "Appointments available from:"

# How long (seconds) to wait for the availability text to appear
PAGE_LOAD_TIMEOUT = 30

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
    """
    Opens the HotDoc page and waits for the text:
      "Appointments available from: 3 Aug, 2:30 pm"
    then parses and returns that date.
    """
    driver = _make_driver()
    try:
        log.info("Navigating to HotDoc page …")
        driver.get(HOTDOC_URL)

        # Wait until the availability label appears in the page
        log.info("Waiting for '%s' to appear …", AVAILABILITY_LABEL)
        try:
            WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(
                EC.text_to_be_present_in_element(
                    (By.TAG_NAME, "body"), AVAILABILITY_LABEL
                )
            )
        except TimeoutException:
            log.warning("Timed out waiting for availability text.")
            # Save screenshot for debugging
            driver.save_screenshot("debug_screenshot.png")
            log.info("Debug screenshot saved.")
            # Log what the page actually says
            try:
                snippet = driver.find_element(By.TAG_NAME, "body").text[:800]
                log.info("Page text snippet:\n%s", snippet)
            except Exception:
                pass
            return None

        # Extract the full page text and find the date after the label
        page_text = driver.find_element(By.TAG_NAME, "body").text
        log.info("Page loaded successfully.")

        idx = page_text.find(AVAILABILITY_LABEL)
        if idx == -1:
            log.error("Label not found in page text despite wait succeeding.")
            return None

        # Grab the text immediately after the label (up to ~40 chars)
        after_label = page_text[idx + len(AVAILABILITY_LABEL):].strip()[:40]
        log.info("Text after label: '%s'", after_label)

        # Parse "3 Aug, 2:30 pm" or "3 Aug" etc.
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
            log.info("Debug screenshot saved.")
        except Exception:
            pass
        return None
    finally:
        driver.quit()


def _parse_hotdoc_date(text: str) -> datetime | None:
    """
    Parse HotDoc's availability date string.
    Examples seen:
      "3 Aug, 2:30 pm"
      "3 Aug"
      "14 Jul, 9:00 am"
      "3 Aug, 2:30 pm..."  (may have trailing text)
    """
    # Clean up trailing punctuation/newlines
    text = text.split("\n")[0].strip().rstrip(".")

    # Regex: "3 Aug, 2:30 pm" or "3 Aug"
    pattern = re.compile(
        r"(\d{1,2})\s+(\w+)"          # day + month name
        r"(?:,\s*(\d{1,2}:\d{2})\s*([aApP][mM]))?",  # optional time
        re.IGNORECASE,
    )
    m = pattern.search(text)
    if not m:
        return None

    day   = m.group(1)
    month = m.group(2)
    ttime = m.group(3)  # e.g. "2:30"
    ampm  = m.group(4)  # e.g. "pm"

    year = datetime.now().year
    # If the month looks like it's already passed this year, use next year
    try:
        test = datetime.strptime(f"{day} {month} {year}", "%d %b %Y")
        if test < datetime.now():
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

    new_str   = new_date.strftime("%-d %b %Y at %-I:%M %p") if new_date.hour else new_date.strftime("%-d %b %Y")
    worst_str = worst_date.strftime("%-d %b %Y at %-I:%M %p") if worst_date.hour else worst_date.strftime("%-d %b %Y")

    subject = f"🗓 Earlier appointment available – {new_str}"
    body = (
        f"An earlier appointment with Dr Lorna Montgomery is now available!\n\n"
        f"  New slot  : {new_str}\n"
        f"  Previous  : {worst_str}\n\n"
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
        log.info("First run – recording WORST as %s", next_appt.date())
        save_worst(next_appt)
        return

    if next_appt >= worst:
        if next_appt > worst:
            log.info("Slot moved later (%s → %s) – updating WORST.", worst.date(), next_appt.date())
            save_worst(next_appt)
        else:
            log.info("No change (%s = WORST) – nothing to do.", next_appt.date())
        return

    # next_appt < worst → strictly earlier slot found!
    log.info("🎉 Earlier slot found: %s < WORST %s", next_appt.date(), worst.date())
    send_alert(next_appt, worst)
    # WORST intentionally NOT updated – keeps alerting until you book.


if __name__ == "__main__":
    main()
