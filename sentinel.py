#!/usr/bin/env python3
"""
Appointment Sentinel
Monitors HotDoc for the next available appointment and emails you
when an earlier slot becomes available than the one previously recorded.

State is persisted in GitHub Actions Repository Variables (not files),
so no commits are needed and there is no superfluous commit history.

Required GitHub Actions Variables (auto-created/updated at runtime):
  SENTINEL_WORST       - ISO datetime of the latest known appointment
  SENTINEL_NEXT_APPOINTMENT - ISO datetime of the latest scraped appointment
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
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlparse, parse_qs, unquote
from zoneinfo import ZoneInfo

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
    "https://www.hotdoc.com.au/search?filters=specialty-27&in=blackbutt-QLD-4306&query=Montgomery"
)

PAGE_LOAD_TIMEOUT = 30
LINK_DETECT_TIMEOUT = 15  # Separate timeout for booking link detection
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
VAR_NEXT_APPOINTMENT = "SENTINEL_NEXT_APPOINTMENT"
VAR_LAST_EMAIL = "SENTINEL_LAST_EMAIL"

BRISBANE = ZoneInfo("Australia/Brisbane")

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


def _ensure_aware_datetime(dt: datetime) -> datetime:
    """
    Express a datetime in Brisbane local time.

    Older values without an offset are interpreted as Brisbane local time;
    values with an offset are converted to Brisbane time before comparison
    and persistence.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=BRISBANE)
    return dt.astimezone(BRISBANE)


def load_worst() -> datetime | None:
    raw = _get_variable(VAR_WORST)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        return _ensure_aware_datetime(dt)
    except ValueError:
        return None


def save_worst(dt: datetime) -> None:
    # Ensure we store timezone-aware datetimes
    dt = _ensure_aware_datetime(dt)
    _set_variable(VAR_WORST, dt.isoformat())
    log.info("WORST saved -> %s", dt)


def save_next_appointment(dt: datetime) -> None:
    """Persist the latest successfully scraped appointment datetime."""
    dt = _ensure_aware_datetime(dt)
    _set_variable(VAR_NEXT_APPOINTMENT, dt.isoformat())
    log.info("NEXT_APPOINTMENT saved -> %s", dt)


def load_last_email() -> dict | None:
    raw = _get_variable(VAR_LAST_EMAIL)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def save_last_email(new_slot: datetime, previous: datetime) -> None:
    # Ensure we store timezone-aware datetimes
    new_slot = _ensure_aware_datetime(new_slot)
    previous = _ensure_aware_datetime(previous)
    payload = json.dumps({
        "new_slot": new_slot.isoformat(),
        "previous": previous.isoformat(),
        "sent_at": datetime.now(timezone.utc).isoformat(),
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

    hours_since = (datetime.now(timezone.utc) - last_sent_at).total_seconds() / 3600
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


def _wait_for_booking_link(driver, timeout: int = 15):
    """
    Wait for booking link to be populated with href attribute.
    Uses JavaScript-based polling to detect when the link is actually ready.
    """
    def link_has_href(driver):
        """Wait for any link with 'appointment' in href to be present."""
        try:
            # Try finding link with appointment in href
            link = driver.find_element(
                By.XPATH,
                "//a[contains(@href, 'appointment') and contains(@href, 'when=')]"
            )
            href = link.get_attribute("href")
            if href and "when=" in href:
                log.info("Found appointment link via XPath")
                return link
            return False
        except:
            return False

    try:
        # First attempt: XPath with href check (more flexible)
        link = WebDriverWait(driver, timeout).until(link_has_href)
        return link
    except TimeoutException:
        log.warning("XPath-based link detection timed out, trying fallback selectors...")
        
        # Fallback 1: Look for any link in AvailabilityRow-action
        try:
            link = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "[class*='AvailabilityRow-action'] a[href*='appointment']")
                )
            )
            log.info("Found appointment link via AvailabilityRow-action")
            return link
        except TimeoutException:
            pass
        
        # Fallback 2: Look for any link with 'when=' parameter
        try:
            link = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "a[href*='when=']")
                )
            )
            log.info("Found appointment link via when= parameter")
            return link
        except TimeoutException:
            pass
        
        raise TimeoutException("Could not find booking link with any selector")


def get_next_appointment() -> datetime | None:
    driver = _make_driver()
    try:
        log.info("Navigating to HotDoc page ...")
        driver.get(HOTDOC_URL)

        log.info("Waiting for appointment availability to load ...")
        try:
            # Wait for the AvailabilityRow-label (date) to appear
            date_label = WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(
                EC.presence_of_element_located(
                    (By.CLASS_NAME, "AvailabilityRow-label")
                )
            )
            log.info("Date label found, extracting text ...")
            date_text = date_label.text.strip()
            
        except TimeoutException:
            log.error("Timed out waiting for appointment date label.")
            _save_debug_artifacts(driver)
            raise RuntimeError(
                f"Failed to load appointment date label within {PAGE_LOAD_TIMEOUT}s. "
                "Check debug_screenshot.png and debug_page_source.html for details."
            )

        log.info("Found appointment date: '%s'", date_text)
        
        # Now try to find the booking link with separate timeout
        try:
            booking_link = _wait_for_booking_link(driver, timeout=LINK_DETECT_TIMEOUT)
            href = booking_link.get_attribute("href")
            
        except TimeoutException:
            log.error("Timed out waiting for booking link.")
            _save_debug_artifacts(driver)
            raise RuntimeError(
                f"Failed to load booking link within {LINK_DETECT_TIMEOUT}s. "
                "The page may be slow to render appointment links. "
                "Check debug_screenshot.png and debug_page_source.html for details."
            )
        
        log.info("Found booking link: %s", href)
        
        # Extract datetime from the booking link's 'when' parameter
        dt = _parse_booking_link(href)
        if dt:
            log.info("Parsed appointment datetime from link: %s", dt)
        else:
            log.error("Could not parse datetime from booking link: %s", href)
            _save_debug_artifacts(driver)
            raise RuntimeError(
                f"Failed to parse appointment datetime from booking link: {href}"
            )
        return dt

    except RuntimeError:
        # Re-raise known errors (timeout, parse failures) with our error message
        raise
    except Exception as exc:
        log.error("Scrape error: %s", exc)
        log.error(traceback.format_exc())
        _save_debug_artifacts(driver)
        raise
    finally:
        driver.quit()


def _save_debug_artifacts(driver) -> None:
    """Save screenshot and page source for debugging."""
    try:
        driver.save_screenshot("debug_screenshot.png")
        log.info("Screenshot saved to debug_screenshot.png")
    except Exception as exc:
        log.warning("Failed to save screenshot: %s", exc)
    
    try:
        page_source = driver.page_source
        with open("debug_page_source.html", "w", encoding="utf-8") as f:
            f.write(page_source)
        log.info("Page source saved to debug_page_source.html (%d bytes)", len(page_source))
    except Exception as exc:
        log.warning("Failed to save page source: %s", exc)
    
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text[:1000]
        log.info("Page text (first 1000 chars):\n%s", body_text)
    except Exception as exc:
        log.warning("Failed to extract page text: %s", exc)


def _parse_booking_link(href: str) -> datetime | None:
    """
    Extract the appointment datetime from the booking link's 'when' parameter.
    
    The 'when' parameter contains a URL-encoded ISO 8601 datetime string.
    Example: when=2026-09-21T15%3A15%3A00%2B10%3A00
    Decoded: when=2026-09-21T15:15:00+10:00
    """
    try:
        parsed_url = urlparse(href)
        query_params = parse_qs(parsed_url.query)
        
        if "when" not in query_params:
            log.warning("No 'when' parameter found in booking link")
            return None
        
        when_value = query_params["when"][0]
        log.info("Extracted 'when' parameter: %s", when_value)
        
        # Parse the ISO 8601 datetime string
        dt = datetime.fromisoformat(when_value)
        # Ensure it's timezone-aware
        return _ensure_aware_datetime(dt)
        
    except (ValueError, KeyError, IndexError) as exc:
        log.warning("Failed to parse booking link datetime: %s", exc)
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

    save_next_appointment(next_appt)

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
