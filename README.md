# Appointment Sentinel

## Cloudflare migration

The Cloudflare Worker implementation in `src/index.ts` is the migration path
from the original GitHub Actions implementation in `sentinel.py`. It fetches
HotDoc on a Cloudflare Cron Trigger, stores state and the public snapshot in
R2, and serves the snapshot at `/appointments.json`.

Local checks:

```bash
npm install
npm run typecheck
npm test
npx wrangler dev --local --var RUN_TOKEN:test-token
curl http://127.0.0.1:8787/health
curl -X POST -H 'Authorization: Bearer test-token' http://127.0.0.1:8787/run
curl http://127.0.0.1:8787/appointments.json | jq .
```

Email alerts use Cloudflare Email Service through the native `EMAIL` binding.
The binding is restricted to `sentinel@timdurrant.com` sending to
`tim.j.durrant@gmail.com`. The protected `/test-email` endpoint is available
for verifying delivery after deployment. Unhandled scheduled-job failures also
send a failure email, with a 24-hour cooldown during a prolonged outage.

Execution history is available at `/logs` on the Worker URL, with the raw
three-entry feed at `/logs.json`.

The legacy workflow remains in `.github/workflows/publish-appointments.yml`
until the Cloudflare Worker has been tested in production.

Appointment Sentinel checks HotDoc for the next available appointment, emails
when Dr Lorna Montgomery has an earlier slot, and publishes a compact JSON
snapshot for other tools.

## Runtime flow

1. Google Cloud Scheduler dispatches the GitHub Actions workflow every 30
   minutes on weekdays during the configured Brisbane hours.
2. `sentinel.py` makes one unauthenticated HotDoc API request.
3. The response is used both for Lorna's existing notification logic and for the
   all-doctors appointment map.
4. The compact map is saved to the `SENTINEL_NEXT_APPOINTMENT_ALL` repository
   variable and deployed as GitHub Pages.

Scheduler configuration: [Appointment Sentinel Cloud Scheduler job](https://console.cloud.google.com/cloudscheduler)

Published appointment data:

Each doctor is represented by an object containing the original ISO timestamp,
a Brisbane-local display value, and the timezone used for that display. Doctors
without an available appointment remain present with null appointment fields.

```bash
curl --fail --silent --compressed \
  https://tim-durrant.github.io/appointment-sentinel/appointments.json \
  | jq .
```

For a simple one-line summary:

```bash
curl --fail --silent --compressed \
  https://tim-durrant.github.io/appointment-sentinel/appointments.json \
  | jq -r 'to_entries[] | "\(.key): \(.value.display // "No appointment") (\(.value.timezone))"'
```

Example:

```json
{
  "Lorna Montgomery": {
    "startDate": "2026-10-12T11:45:00+10:00",
    "display": "12 October 2026 at 11:45 AM",
    "timezone": "Australia/Brisbane"
  }
}
```


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

The Pages deployment also requires GitHub Pages to use **GitHub Actions** as its
build and deployment source.

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
