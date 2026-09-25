# Appointment Sentinel

Appointment Sentinel checks HotDoc for the next available appointment, emails
when Dr Lorna Montgomery has an earlier slot, and publishes a compact JSON
snapshot for other tools.

## Runtime flow

1. Google Cloud Scheduler dispatches the GitHub Actions workflow every 30
   minutes on weekdays during the configured Brisbane hours.
2. `sentinel.py` makes one unauthenticated HotDoc API request.
3. The response is used both for Lorna's existing notification logic and for
   the all-doctors appointment map.
4. The compact map is saved to the `SENTINEL_NEXT_APPOINTMENT_ALL` repository
   variable and deployed as GitHub Pages.

Scheduler configuration: [Appointment Sentinel Cloud Scheduler job](https://console.cloud.google.com/cloudscheduler)

Published appointment data:

```bash
curl --fail --silent \
  https://tim-durrant.github.io/appointment-sentinel/appointments.json
```

## State and secrets

Notification state is stored in GitHub Actions repository variables:

- `SENTINEL_WORST`
- `SENTINEL_NEXT_APPOINTMENT`
- `SENTINEL_LAST_EMAIL`
- `SENTINEL_NEXT_APPOINTMENT_ALL`

Configure these repository secrets under **Settings → Secrets and variables →
Actions**:

| Secret | Purpose |
|---|---|
| `SMTP_HOST` | SMTP server, normally `smtp.gmail.com` |
| `SMTP_PORT` | SMTP port, normally `587` |
| `SMTP_USER` | SMTP account |
| `SMTP_PASSWORD` | SMTP app password |
| `ALERT_TO` | Notification recipient |
| `GH_PAT` | Fine-grained token allowed to read/write repository variables |

The Pages deployment also requires GitHub Pages to use **GitHub Actions** as
its build and deployment source.

## Files

```text
appointment-sentinel/
├── .github/workflows/publish-appointments.yml
├── sentinel.py
├── requirements.txt
└── README.md
```

The workflow is manually dispatchable for testing. Cloud Scheduler is the
production scheduler, so the workflow itself has no GitHub cron schedule.

## Local checks

Install dependencies and run syntax validation with:

```bash
python -m pip install -r requirements.txt
python -m py_compile sentinel.py
```
