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
  SENTINEL_NEXT_APPOINTMENT_ALL - compact JSON map of all doctors' appointments

Required GitHub Actions Secrets:
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, ALERT_TO
  GH_REPO              - e.g. "timjdurrant/appointment-sentinel"
  GH_PAT               - Fine-grained PAT with "Variables" read/write permission
"""
import json
import os
import smtplib
import socket
import logging
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

HOTDOC_URL = (
    "https://www.hotdoc.com.au/search?filters=specialty-27&in=blackbutt-QLD-4306&query=Montgomery"
)
HOTDOC_API_URL = (
    "https://www.hotdoc.com.au/api/patient/pages?path="
    "%252Fmedical-centres%252Fblackbutt-QLD-4306"
    "%252Fblackbutt-medical-centre%252Fdoctors"
)
HOTDOC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/26.6.2 Safari/605.1.15"
    ),
    "Accept": "application/au.com.hotdoc.v6",
    "app-timezone": "Australia/Brisbane",
}
TARGET_DOCTOR = "Lorna Montgomery"

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
VAR_NEXT_APPOINTMENT_ALL = "SENTINEL_NEXT_APPOINTMENT_ALL"

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
# HOTDOC API
# ---------------------------------------------------------------------------

def fetch_appointments() -> dict[str, str | None]:
    """Fetch the compact next-appointment map for every doctor."""
    response = requests.get(
        HOTDOC_API_URL,
        headers=HOTDOC_HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()

    employees = payload["page"]["metadata"]["schema"]["employee"]
    if not isinstance(employees, list) or not employees:
        raise RuntimeError("HotDoc response contained no doctors")

    appointments: dict[str, str | None] = {}
    for employee in employees:
        if not isinstance(employee, dict):
            continue

        name = employee.get("name")
        if not isinstance(name, str) or not name:
            continue

        if name.startswith("Dr. "):
            name = name[4:]
        elif name.startswith("Dr "):
            name = name[3:]

        performer_in = employee.get("performerIn") or {}
        start_date = (
            performer_in.get("startDate")
            if isinstance(performer_in, dict)
            else None
        )
        appointments[name] = start_date if isinstance(start_date, str) else None

    if not appointments:
        raise RuntimeError("HotDoc response contained no named doctors")

    return appointments


def _save_appointments_variable(value: str) -> None:
    """Persist the compact all-doctors JSON in the repository variable."""
    _set_variable(VAR_NEXT_APPOINTMENT_ALL, value)


def _write_appointments_file(value: str) -> None:
    """Write the compact all-doctors JSON for the GitHub Pages artifact."""
    output_path = os.getenv("APPOINTMENTS_OUTPUT")
    if not output_path:
        return

    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as output:
        output.write(value + "\n")


def get_next_appointment(appointments: dict[str, str | None]) -> datetime | None:
    """Return the target doctor's next appointment from the API response."""
    raw_value = appointments.get(TARGET_DOCTOR)
    if not raw_value:
        raise RuntimeError(f"HotDoc response contained no appointment for {TARGET_DOCTOR}")

    try:
        return _ensure_aware_datetime(datetime.fromisoformat(raw_value))
    except ValueError as exc:
        raise RuntimeError(
            f"Invalid appointment datetime for {TARGET_DOCTOR}: {raw_value}"
        ) from exc


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

    appointments = fetch_appointments()
    compact_json = json.dumps(
        appointments,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    _save_appointments_variable(compact_json)
    _write_appointments_file(compact_json)
    log.info("Published %d doctors", len(appointments))

    next_appt = get_next_appointment(appointments)

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
