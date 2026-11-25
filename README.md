# Appointment Sentinel

## Cloudflare migration

The Cloudflare Worker implementation in `src/index.ts` fetches HotDoc on a
Cloudflare Cron Trigger, stores state, the public snapshot, and recent
execution logs in R2, and serves them from the Worker URL.

Local checks:

```bash
npm install
npm run typecheck
npm test
npx wrangler dev --local --var RUN_TOKEN:test-token
curl --fail http://127.0.0.1:8787/health
curl --fail http://127.0.0.1:8787/appointments.json | jq .
curl --fail http://127.0.0.1:8787/logs.json | jq .

# Protected local test endpoints
curl --fail -X POST -H 'Authorization: Bearer test-token' http://127.0.0.1:8787/run
curl --fail -X POST -H 'Authorization: Bearer test-token' http://127.0.0.1:8787/test-email
```

Email alerts use Cloudflare Email Service through the native `EMAIL` binding.
The binding is restricted to `sentinel@timdurrant.com` sending to
`tim.j.durrant@gmail.com`. The protected `/test-email` endpoint is available
for verifying delivery after deployment. Unhandled scheduled-job failures also
send a failure email, with a 24-hour cooldown during a prolonged outage.

Execution history is available at `/logs` on the Worker URL, with the raw
three-entry feed at `/logs.json`.

Appointment Sentinel checks HotDoc for the next available appointment, emails
when Dr Lorna Montgomery has an earlier slot, and publishes a compact JSON
snapshot for other tools.

## Runtime flow

1. The Cloudflare Cron Trigger invokes the Worker every day at the configured
   Brisbane monitoring times.
2. The Worker makes an unauthenticated HotDoc API request.
3. The response is used for Lorna's notification logic and the all-doctors
   appointment map.
4. The compact map, state, and recent execution logs are saved in R2.

Current cron expression in `wrangler.toml`:

```text
17,47 0-8 * * *
```

This runs every day at 17 and 47 minutes past each UTC hour from 00:00 through
08:00, which is 10:17 through 18:47 Brisbane time. The trigger can be viewed or
edited under **Workers & Pages → appointment-sentinel → Settings → Triggers →
Cron triggers**.

Published appointment data:

Each doctor is represented by an object containing the original ISO timestamp,
a Brisbane-local display value, and the timezone used for that display. Doctors
without an available appointment remain present with null appointment fields.

```bash
curl --fail --silent --compressed \
  https://appointment-sentinel.tim-j-durrant.workers.dev/appointments.json \
  | jq .
```

For a simple one-line summary:

```bash
curl --fail --silent --compressed \
  https://appointment-sentinel.tim-j-durrant.workers.dev/appointments.json \
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


## Files

```text
appointment-sentinel/
├── src/index.ts
├── wrangler.toml
├── package.json
├── test/sentinel.test.ts
└── README.md
```

The old Python and GitHub Actions files are retained as migration history; the
Cloudflare Worker is the production implementation.
