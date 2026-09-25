#!/usr/bin/env python3
"""
Fetch the next available appointment for every doctor at Blackbutt Medical Centre
and persist the result in a GitHub Actions repository variable.
"""

import json
import os
from typing import Any

import requests

HOTDOC_URL = (
    "https://www.hotdoc.com.au/api/patient/pages"
    "?path=%252Fmedical-centres%252Fblackbutt-QLD-4306"
    "%252Fblackbutt-medical-centre%252Fdoctors"
)

VAR_NEXT_APPOINTMENT_ALL = "SENTINEL_NEXT_APPOINTMENT_ALL"
GH_REPO = os.environ["GH_REPO"]
GH_PAT = os.environ["GH_PAT"]

GH_API_BASE = f"https://api.github.com/repos/{GH_REPO}/actions/variables"
GH_HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {GH_PAT}",
    "X-GitHub-Api-Version": "2026-03-10",
}


def fetch_appointments() -> dict[str, str | None]:
    response = requests.get(
        HOTDOC_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/26.6.2 Safari/605.1.15"
            ),
            "Accept": "application/au.com.hotdoc.v6",
            "app-timezone": "Australia/Brisbane",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload: dict[str, Any] = response.json()

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


def save_variable(value: str) -> None:
    url = f"{GH_API_BASE}/{VAR_NEXT_APPOINTMENT_ALL}"

    response = requests.patch(
        url,
        headers=GH_HEADERS,
        json={"name": VAR_NEXT_APPOINTMENT_ALL, "value": value},
        timeout=15,
    )

    if response.status_code == 404:
        response = requests.post(
            GH_API_BASE,
            headers=GH_HEADERS,
            json={"name": VAR_NEXT_APPOINTMENT_ALL, "value": value},
            timeout=15,
        )

    response.raise_for_status()


def write_output_file(value: str) -> None:
    output_path = os.getenv("APPOINTMENTS_OUTPUT")
    if not output_path:
        return

    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as output:
        output.write(value + "\n")


def main() -> None:
    appointments = fetch_appointments()
    value = json.dumps(
        appointments,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    save_variable(value)
    write_output_file(value)
    print(f"Saved {len(appointments)} doctors to {VAR_NEXT_APPOINTMENT_ALL}")


if __name__ == "__main__":
    main()
