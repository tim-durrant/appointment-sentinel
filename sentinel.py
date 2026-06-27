#!/usr/bin/env python3
"""
Appointment Sentinel
Monitors HotDoc for the next available appointment and emails you
when an earlier slot becomes available than the one previously recorded.

Designed to run as a GitHub Actions scheduled job.
State (the WORST date) is persisted in state/worst.json, which is committed
back to the repository after each run by the workflow.

HotDoc is a JavaScript-rendered Ember.js SPA. The scraping strategy is:
  1. Open the page in headless Chrome and wait for it to fully render.
  2. Intercept the XHR/fetch calls the page makes to HotDoc's API to grab
     the availability data directly from the network response.
  3. Fall back to scanning rendered DOM text if the network intercept misses.
"""

import json
import os
import re
import smtplib
import logging
import sys
import traceback
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

# How long (seconds) to wait for the page to render appointment data.
PAGE_LOAD_TIMEOUT = 30

# Email – set via GitHub Actions secrets (see README)
SMTP_HOST     = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
ALERT_TO      = os.getenv("ALERT_TO", "")

# Persisted state file (committed back to repo by the workflow)
STATE_FILE = Path("state/worst.json")

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
    """Create a headless Chrome driver with network logging enabled."""
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    # Enable browser-level network logging so we can intercept API responses.
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    return webdriver.Chrome(options=opts)


def _dates_from_network_log(driver: webdriver.Chrome) -> list[datetime]:
    """
    Pull network responses from the Chrome performance log and look for
    HotDoc API responses that contain appointment availability data.
    """
    dates: list[datetime] = []
    try:
        logs = driver.get_log("performance")
    except Exception:
        return dates

    for entry in logs:
        try:
            msg = json.loads(entry["message"])["message"]
            # We only care about Network responses that contain body data.
            if msg.get("method") != "Network.responseReceived":
                continue
            url = msg.get("params", {}).get("response", {}).get("url", "")
            # HotDoc availability calls typically hit /api/... or /available_times
            if not any(kw in url for kw in ("available", "appointment", "slot", "timeslot", "booking")):
                continue
            # Fetch the response body via CDP
            request_id = msg["params"]["requestId"]
            body_result = driver.execute_cdp_cmd(
                "Network.getResponseBody", {"requestId": request_id}
            )
            body = body_result.get("body", "")
            if not body:
                continue
            log.info("Inspecting API response from: %s", url)
            # Try to parse as JSON and hunt for date strings
            try:
                data = json.loads(body)
                dates.extend(_extract_dates_from_json(data))
            except json.JSONDecodeError:
                dates.extend(_extract_dates_from_text(body))
        except Exception:
            continue

    return dates


def _extract_dates_from_json(obj, depth: int = 0) -> list[datetime]:
    """Recursively walk a JSON object looking for ISO date strings."""
    if depth > 10:
        return []
    dates = []
    if isinstance(obj, str):
        dt = _try_parse(obj)
        if dt:
            dates.append(dt)
    elif isinstance(obj, list):
        for item in obj:
            dates.extend(_extract_dates_from_json(item, depth + 1))
    elif isinstance(obj, dict):
        for v in obj.values():
            dates.extend(_extract_dates_from_json(v, depth + 1))
    return dates


def _dom_dates(driver: webdriver.Chrome) -> list[datetime]:
    """
    Scan the rendered DOM for date-like text. Works as a fallback when
    the network intercept doesn't capture anything useful.
    Looks for common HotDoc slot patterns like "Mon 14 Jul" or "9:00 AM".
    """
    dates: list[datetime] = []

    # Strategy A: look for time elements with datetime attributes
    try:
        time_els = driver.find_elements(By.TAG_NAME, "time")
        for el in time_els:
            raw = el.get_attribute("datetime") or el.text or ""
            dt = _try_parse(raw.strip())
            if dt:
                dates.append(dt)
    except Exception:
        pass

    # Strategy B: look for buttons / divs that contain date text
    try:
        candidates = driver.find_elements(
            By.CSS_SELECTOR,
            "button, [role='button'], [class*='slot'], [class*='time'], [class*='avail']"
        )
        for el in candidates:
            for attr in ("datetime", "data-datetime", "aria-label", "title", "data-date"):
                raw = (el.get_attribute(attr) or "").strip()
                dt = _try_parse(raw)
                if dt:
                    dates.append(dt)
            # Also check visible text
            dt = _try_parse((el.text or "").strip())
            if dt:
                dates.append(dt)
    except Exception:
        pass

    # Strategy C: full page text scan
    if not dates:
        try:
            page_text = driver.find_element(By.TAG_NAME, "body").text
            dates.extend(_extract_dates_from_text(page_text))
        except Exception:
            pass

    return dates


def get_next_appointment() -> datetime | None:
    """
    Open the HotDoc page, let it render, then extract the earliest available
    appointment date using network intercept + DOM fallback.
    Returns None if nothing could be found.
    """
    driver = _make_driver()
    try:
        # Enable CDP network tracking before navigating
        driver.execute_cdp_cmd("Network.enable", {})

        log.info("Navigating to HotDoc page …")
        driver.get(HOTDOC_URL)

        # Wait for the page to show *something* meaningful.
        # HotDoc renders a loading spinner first; we wait for it to disappear
        # or for any interactive element to appear.
        wait = WebDriverWait(driver, PAGE_LOAD_TIMEOUT)
        try:
            # Wait until the page title changes away from the generic one
            wait.until(lambda d: "Montgomery" in d.title or "Blackbutt" in d.title or
                       len(d.find_elements(By.CSS_SELECTOR, "button, [role='button']")) > 3)
            log.info("Page appears to have rendered. Title: %s", driver.title)
        except TimeoutException:
            log.warning("Timed out waiting for page render – proceeding anyway.")

        # Give JS a moment to finish any final async data fetches
        import time
        time.sleep(5)

        # Dump page source snippet for debugging
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text
            snippet = body_text[:500].replace("\n", " ")
            log.info("Page text snippet: %s", snippet)
        except Exception:
            log.warning("Could not read page body text.")

        # --- Primary strategy: network log ---
        dates = _dates_from_network_log(driver)
        if dates:
            log.info("Found %d date(s) via network intercept.", len(dates))
        else:
            log.info("Network intercept found no dates – trying DOM scan …")
            dates = _dom_dates(driver)
            log.info("DOM scan found %d date(s).", len(dates))

        if dates:
            # Filter out dates in the past
            now = datetime.now()
            future_dates = [d for d in dates if d >= now.replace(hour=0, minute=0, second=0, microsecond=0)]
            if future_dates:
                earliest = min(future_dates)
                log.info("Earliest future slot: %s", earliest.date())
                return earliest
            log.info("All found dates are in the past – ignoring.")

        log.info("No appointment dates found.")
        return None

    except Exception as exc:
        log.error("Scrape error: %s", exc)
        log.error(traceback.format_exc())
        # Save a screenshot for debugging
        try:
            driver.save_screenshot("debug_screenshot.png")
            log.info("Screenshot saved to debug_screenshot.png")
        except Exception:
            pass
        return None
    finally:
        driver.quit()


# ─── DATE PARSING ─────────────────────────────────────────────────────────────

_DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M%z",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%d %b %Y %I:%M %p",
    "%d %b %Y",
    "%A %d %B %Y",
    "%a %d %b %Y",
    "%a %d %b",         # "Mon 14 Jul" – year assumed current
]


def _try_parse(text: str) -> datetime | None:
    text = text.strip()
    if not text or len(text) < 5:
        return None
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            # If no year was parsed, assume current year
            if dt.year == 1900:
                dt = dt.replace(year=datetime.now().year)
            return dt
        except ValueError:
            continue
    return None


def _extract_dates_from_text(text: str) -> list[datetime]:
    pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:?\d{2})?)?)"  # ISO
        r"|(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})"                                            # d/m/Y
        r"|(\d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w* \d{4})",    # d Mon YYYY
        re.IGNORECASE,
    )
    results = []
    for m in pattern.finditer(text):
        raw = m.group(0).strip()
        dt = _try_parse(raw)
        if dt:
            results.append(dt)
    return results

# ─── EMAIL ────────────────────────────────────────────────────────────────────

def send_alert(new_date: datetime, worst_date: datetime) -> None:
    if not all([SMTP_USER, SMTP_PASSWORD, ALERT_TO]):
        log.warning("Email credentials not configured – skipping alert.")
        return

    date_str = new_date.strftime("%-d %b %Y") if new_date.hour == 0 else new_date.strftime("%-d %b %Y at %-I:%M %p")
    subject = f"🗓 Earlier appointment available – {date_str}"
    body = (
        f"An earlier appointment with Dr Lorna Montgomery is now available!\n\n"
        f"  New slot  : {date_str}\n"
        f"  Previous  : {worst_date.strftime('%-d %b %Y')}\n\n"
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
    # WORST is intentionally NOT updated – keeps alerting until you book.


if __name__ == "__main__":
    main()
