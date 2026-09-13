#!/usr/bin/env python3
"""
Appointment Sentinel
Monitors HotDoc for the next available appointment and emails you
when an earlier slot becomes available than the one previously recorded.

State is persisted in GitHub Actions Repository Variables (not files),
so no commits are needed and there is no superfluous commit history.

Required GitHub Actions Variables (auto-created/updated at runtime):
  SENTINEL_WORST       - ISO datetime of the latest known appointment
  SENTINEL_LAST_EMAIL  - JSON blob of last email sent

Required GitHub Actions Secrets:
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, ALERT_TO
  GH_REPO              - e.g. "timjdurrant/appointment-sentinel"
  GH_PAT               - Fine-grained PAT with "Variables" read/write permission
"""
import json
import os
import re
import smtplib
import socket
import logging
import sys
import traceback
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

HOTDOC_URL = (
    "https://www.hotdoc.com.au/request/appointment/doctor-time"
    "?clinic=blackbutt-medical-centre&doctor=lorna-montgomery"
    "&for=family-member&history=return-visit&reason=114047"
)

PAGE_LOAD_TIMEOUT = 30
EMAIL_REPEAT_HOURS = 24
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
ALERT_TO = os.getenv("ALERT_TO", "")

# GitHub API - used for reading/writing repository variables
GH_REPO = os.getenv("GH_REPO", "")  # e.g. "timjdurrant/appointment-sentinel"
GH_PAT = os.getenv("GH_PAT", "")    # Fine-grained PAT

GH_API_BASE = f"https://api.github.com/repos/{GH_REPO}/actions/variables"
GH_HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {GH_PAT}",
    "X-GitHub-Api-Version": "2022-11-28",
}

# Variable names stored in GitHub
VAR_WORST = "SENTINEL_WORST"
VAR_LAST_EMAIL = "SENTINEL_LAST_EMAIL"

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GITHUB VARIABLES STATE
# ---------------------------------------------------------------------------

def _get_variable(name: str) -> str | None:
    """Read a GitHub Actions repository variable. Returns None if not set."""
    try:
        r = requests.get(f"{GH_API_BASE}/{name}", headers=GH_HEADERS, timeout=10)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("value")
    except Exception as exc:
        log.warning("Failed to read variable %s: %s", name, exc)
        return None


def _set_variable(name: str, value: str) -> None:
    """Create or update a GitHub Actions repository variable."""
    try:
        # Try PATCH first (update existing)
        r = requests.patch(
            f"{GH_API_BASE}/{name}",
            headers=GH_HEADERS,
            json={"name": name, "value": value},
            timeout=10,
        )
        if r.status_code == 404:
            # Variable doesn't exist yet - create it
            r = requests.post(
                GH_API_BASE,
                headers=GH_HEADERS,
                json={"name": name, "value": value},
                timeout=10,
            )
        r.raise_for_status()
        log.info("Variable %s saved OK", name)
    except Exception as exc:
        log.error("Failed to save variable %s: %s", name, exc)


def load_worst() -> datetime | None:
    raw = _get_variable(VAR_WORST)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def save_worst(dt: datetime) -> None:
    _set_variable(VAR_WORST, dt.isoformat())
    log.info("WORST saved -> %s", dt)


def load_last_email() -> dict | None:
    raw = _get_variable(VAR_LAST_EMAIL)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def save_last_email(new_slot: datetime, previous: datetime) -> None:
    payload = json.dumps({
        "new_slot": new_slot.isoformat(),
        "previous": previous.isoformat(),
        "sent_at": datetime.now().isoformat(),
    })
    _set_variable(VAR_LAST_EMAIL, payload)


# ---------------------------------------------------------------------------
# EMAIL DEDUPLICATION
# ---------------------------------------------------------------------------

def should_send_email(new_slot: datetime, previous: datetime) -> bool:
    last = load_last_email()

    if last is None:
        log.info("No previous email on record - will send.")
        return True

    last_new_slot = datetime.fromisoformat(last["new_slot"])
    last_previous = datetime.fromisoformat(last["previous"])
    last_sent_at = datetime.fromisoformat(last["sent_at"])

    if (new_slot != last_new_slot) or (previous != last_previous):
        log.info("Email content changed - will send.")
        return True

    hours_since = (datetime.now() - last_sent_at).total_seconds() / 3600
    if hours_since >= EMAIL_REPEAT_HOURS:
        log.info("%.1fh since last identical email - will resend.", hours_since)
        return True

    log.info("Suppressing duplicate email (%.1fh since last send).", hours_since)
    return False


# ---------------------------------------------------------------------------
# SCRAPING
# ---------------------------------------------------------------------------

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
        log.info("Navigating to HotDoc page ...")
        driver.get(HOTDOC_URL)

        log.info("Waiting for appointment date button to appear ...")
        try:
            # Wait for the element AND read its text atomically.
            # This lambda-based approach allows Selenium to retry on stale
            # references rather than crashing mid-operation.
            date_text = WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(
                lambda d: d.find_element(By.CLASS_NAME, "OutsideRangeNav-action").text.strip()
            )
        except TimeoutException:
            log.warning("Timed out waiting for appointment button.")
            driver.save_screenshot("debug_screenshot.png")
            try:
                log.info(
                    "Page text:\n%s",
                    driver.find_element(By.TAG_NAME, "body").text[:800],
                )
            except Exception:
                pass
            return None

        log.info("Found appointment text: '%s'", date_text)

        dt = _parse_hotdoc_date(date_text)
        if dt:
            log.info("Parsed appointment date: %s", dt)
        else:
            log.warning("Could not parse date from: '%s'", date_text)
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
    """
    Parse the two availability formats HotDoc currently exposes:

      "15 Sep, 2:30 pm"  -> explicit calendar date
      "Tue, 3:15 pm"     -> next occurrence of that weekday

    The weekday-only form is used when the appointment falls within HotDoc's
    near-term display window and no month/day-of-month is shown.
    """
    text = text.split("\n")[0].strip().rstrip(".")
    now = datetime.now()

    # Format 1: weekday only, e.g. "Tue, 3:15 pm".
    weekday_match = re.fullmatch(
        r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s*"
        r"(\d{1,2}:\d{2})\s*([ap]m)",
        text,
        re.IGNORECASE,
    )

    if weekday_match:
        weekday_name, appointment_time, ampm = weekday_match.groups()

        weekdays = {
            "mon": 0,
            "tue": 1,
            "wed": 2,
            "thu": 3,
            "fri": 4,
            "sat": 5,
            "sun": 6,
        }

        target_weekday = weekdays[weekday_name.lower()]
        days_ahead = (target_weekday - now.weekday()) % 7
        candidate_date = now + timedelta(days=days_ahead)

        parsed_time = datetime.strptime(
            f"{appointment_time} {ampm.upper()}",
            "%I:%M %p",
        )

        candidate = candidate_date.replace(
            hour=parsed_time.hour,
            minute=parsed_time.minute,
            second=0,
            microsecond=0,
        )

        # If it is already that weekday but the advertised time has passed,
        # HotDoc necessarily means the same weekday next week.
        if candidate <= now:
            candidate += timedelta(days=7)

        return candidate

    # Format 2: explicit date, e.g. "15 Sep, 2:30 pm".
    date_match = re.fullmatch(
        r"(\d{1,2})\s+([A-Za-z]{3,9})"
        r"(?:,\s*(\d{1,2}:\d{2})\s*([ap]m))?",
        text,
        re.IGNORECASE,
    )

    if not date_match:
        return None

    day, month, appointment_time, ampm = date_match.groups()
    year = now.year

    try:
        base_date = datetime.strptime(
            f"{day} {month} {year}",
            "%d %b %Y",
        )

        # HotDoc omits the year. If that calendar date has already passed this
        # year, it refers to the next calendar year.
        if base_date < now.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ):
            year += 1

        if appointment_time and ampm:
            return datetime.strptime(
                f"{day} {month} {year} "
                f"{appointment_time} {ampm.upper()}",
                "%d %b %Y %I:%M %p",
            )

        return datetime.strptime(
            f"{day} {month} {year}",
            "%d %b %Y",
        )

    except ValueError as exc:
        log.warning("strptime failed for '%s': %s", text, exc)
        return None


# ---------------------------------------------------------------------------
# EMAIL
# ---------------------------------------------------------------------------

def send_alert(new_date: datetime, worst_date: datetime) -> None:
    if not all([SMTP_USER, SMTP_PASSWORD, ALERT_TO]):
        log.warning("Email credentials not configured - skipping alert.")
        return

    if not should_send_email(new_slot=new_date, previous=worst_date):
        return

    def fmt(dt: datetime) -> str:
        return (
            dt.strftime("%-d %b %Y at %-I:%M %p")
            if dt.hour or dt.minute
            else dt.strftime("%-d %b %Y")
        )

    subject = f"Earlier appointment available - {fmt(new_date)}"
    body = (
        f"An earlier appointment with Dr Lorna Montgomery is now available!\n\n"
        f"  New slot  : {fmt(new_date)}\n"
        f"  Previous  : {fmt(worst_date)}\n\n"
        f"Book now -> {HOTDOC_URL}\n\n"
        f"- Appointment Sentinel"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Appointment Sentinel <{SMTP_USER}>"
    msg["To"] = ALERT_TO
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()

            # Raises SMTPAuthenticationError if the credentials/app password
            # are missing or invalid.
            server.login(SMTP_USER, SMTP_PASSWORD)

            refused = server.sendmail(
                SMTP_USER,
                ALERT_TO,
                msg.as_string(),
            )

            # sendmail() can return refused recipients instead of raising,
            # especially when only some recipients fail.
            if refused:
                raise RuntimeError(f"SMTP refused recipients: {refused}")

        log.info("Alert sent to %s OK", ALERT_TO)
        save_last_email(new_slot=new_date, previous=worst_date)

    except smtplib.SMTPAuthenticationError:
        log.exception("SMTP authentication failed; check SMTP_USER and the app password")
        raise

    except (smtplib.SMTPException, socket.timeout, OSError):
        log.exception("SMTP error while sending email")
        raise

    except Exception:
        log.exception("Unexpected error while sending email")
        raise


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("=== Appointment Sentinel ===")
    worst = load_worst()
    log.info("Loaded WORST: %s", worst if worst else "None")

    next_appt = get_next_appointment()

    if next_appt is None:
        log.info("No appointment found this run.")
        return

    if worst is None:
        log.info("First run - recording WORST as %s", next_appt)
        save_worst(next_appt)
        return

    if next_appt >= worst:
        if next_appt > worst:
            log.info(
                "Slot moved later (%s -> %s) - updating WORST.",
                worst,
                next_appt,
            )
            save_worst(next_appt)
        else:
            log.info("No change - nothing to do.")
        return

    # next_appt < worst -> earlier slot found!
    log.info("Earlier slot found: %s < WORST %s", next_appt, worst)
    send_alert(next_appt, worst)

    # WORST intentionally NOT updated - keeps alerting until you book.


if __name__ == "__main__":
    main()
