const STATE_KEY = "state.json";
const APPOINTMENTS_KEY = "appointments.json";
const LOGS_KEY = "logs.json";

const DEFAULT_HOTDOC_HEADERS = {
  "User-Agent": "Mozilla/5.0 Appointment Sentinel/1.0",
  Accept: "application/au.com.hotdoc.v6",
  "app-timezone": "Australia/Brisbane",
};

type AppointmentMap = Record<string, string | null>;
type PublishedAppointment = {
  startDate: string | null;
  display: string | null;
  timezone: string;
};
type PublishedMap = Record<string, PublishedAppointment>;
type EmailRecord = {
  newSlot: string;
  previous: string;
  sentAt: string;
};
type SentinelState = {
  worst: string | null;
  lastEmail: EmailRecord | null;
  lastFailureEmailAt: string | null;
};
type RunLog = {
  startedAt: string;
  finishedAt: string;
  durationMs: number;
  trigger: "scheduled" | "manual";
  status: "success" | "failure";
  doctors?: number;
  nextAppointment?: string | null;
  error?: string;
};

export interface Env {
  APPOINTMENT_DATA: R2Bucket;
  HOTDOC_API_URL: string;
  HOTDOC_BOOKING_URL: string;
  TARGET_DOCTOR: string;
  TIMEZONE: string;
  EMAIL_REPEAT_HOURS: string;
  SENDER_ADDRESS: string;
  ALERT_TO: string;
  FAILURE_REPEAT_HOURS: string;
  EMAIL: EmailSender;
  RUN_TOKEN?: string;
}

type EmailSender = {
  send(message: {
    to: string;
    from: string;
    subject: string;
    text: string;
    html?: string;
  }): Promise<{ messageId: string }>;
};

export default {
  async scheduled(_event: ScheduledEvent, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(runCheckWithFailureAlert(env, "scheduled"));
  },

  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/health") {
      return Response.json({ ok: true, service: "appointment-sentinel" });
    }

    if (request.method === "GET" && url.pathname === "/appointments.json") {
      return getPublishedAppointments(env, url.searchParams.get("doctor"));
    }

    if (request.method === "GET" && url.pathname === "/logs") {
      return getLogsPage(env);
    }

    if (request.method === "GET" && url.pathname === "/logs.json") {
      return Response.json(await readLogs(env.APPOINTMENT_DATA), {
        headers: { "Cache-Control": "no-store" },
      });
    }

    if (request.method === "POST" && url.pathname === "/run") {
      if (!env.RUN_TOKEN || request.headers.get("Authorization") !== `Bearer ${env.RUN_TOKEN}`) {
        return new Response("Unauthorized", { status: 401 });
      }
      await runCheckWithFailureAlert(env, "manual");
      return Response.json({ ok: true });
    }

    if (request.method === "POST" && url.pathname === "/test-email") {
      if (!env.RUN_TOKEN || request.headers.get("Authorization") !== `Bearer ${env.RUN_TOKEN}`) {
        return new Response("Unauthorized", { status: 401 });
      }
      const messageId = await sendTestEmail(env);
      return Response.json({ ok: true, messageId });
    }

    return new Response("Not found", { status: 404 });
  },
};

export async function runCheck(env: Env, now = new Date()): Promise<{ doctors: number; next: string | null }> {
  console.log("[sentinel] check started", { at: now.toISOString() });
  const state = await readState(env.APPOINTMENT_DATA);
  const appointments = await fetchAppointments(env.HOTDOC_API_URL);
  const published = buildPublishedAppointments(appointments, env.TIMEZONE);

  await env.APPOINTMENT_DATA.put(APPOINTMENTS_KEY, JSON.stringify(published), {
    httpMetadata: { contentType: "application/json; charset=utf-8", cacheControl: "no-store" },
  });

  const next = getNextAppointment(appointments, env.TARGET_DOCTOR, env.TIMEZONE);
  const nextState = await updateState(state, next, env, now);
  await writeState(env.APPOINTMENT_DATA, nextState);
  console.log("[sentinel] check completed", { doctors: Object.keys(appointments).length, next });
  return { doctors: Object.keys(appointments).length, next };
}

async function runCheckWithFailureAlert(env: Env, trigger: RunLog["trigger"]): Promise<void> {
  const startedAt = new Date();
  try {
    const result = await runCheck(env);
    await recordRunLog(env.APPOINTMENT_DATA, {
      startedAt: startedAt.toISOString(),
      finishedAt: new Date().toISOString(),
      durationMs: Date.now() - startedAt.valueOf(),
      trigger,
      status: "success",
      doctors: result.doctors,
      nextAppointment: result.next,
    });
  } catch (error) {
    console.error("[sentinel] check failed", error);
    try {
      await sendFailureAlert(error, env);
    } catch (notificationError) {
      console.error("[sentinel] failure alert could not be sent", notificationError);
    }
    await recordRunLog(env.APPOINTMENT_DATA, {
      startedAt: startedAt.toISOString(),
      finishedAt: new Date().toISOString(),
      durationMs: Date.now() - startedAt.valueOf(),
      trigger,
      status: "failure",
      error: error instanceof Error ? `${error.name}: ${error.message}` : String(error),
    });
    throw error;
  }
}

async function fetchAppointments(apiUrl: string): Promise<AppointmentMap> {
  let lastError: unknown;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 25_000);
    try {
      const response = await fetch(apiUrl, {
        headers: DEFAULT_HOTDOC_HEADERS,
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`HotDoc returned HTTP ${response.status}`);
      return parseAppointments(await response.json());
    } catch (error) {
      lastError = error;
      console.warn("[sentinel] HotDoc attempt failed", { attempt, error: String(error) });
      if (attempt < 3) await new Promise((resolve) => setTimeout(resolve, attempt * 1_000));
    } finally {
      clearTimeout(timeout);
    }
  }
  throw lastError instanceof Error ? lastError : new Error(String(lastError));
}

function parseAppointments(payload: unknown): AppointmentMap {
  const employees = (payload as { page?: { metadata?: { schema?: { employee?: unknown } } } })
    ?.page?.metadata?.schema?.employee;
  if (!Array.isArray(employees) || employees.length === 0) {
    throw new Error("HotDoc response contained no doctors");
  }

  const appointments: AppointmentMap = {};
  for (const employee of employees) {
    if (!employee || typeof employee !== "object") continue;
    const record = employee as { name?: unknown; performerIn?: { startDate?: unknown } | null };
    if (typeof record.name !== "string" || !record.name) continue;
    const name = record.name.replace(/^Dr\.?\s+/, "");
    const startDate = record.performerIn?.startDate;
    appointments[name] = typeof startDate === "string" ? startDate : null;
  }
  if (Object.keys(appointments).length === 0) throw new Error("HotDoc response contained no named doctors");
  return appointments;
}

export function buildPublishedAppointments(appointments: AppointmentMap, timezone: string): PublishedMap {
  return Object.fromEntries(Object.entries(appointments).map(([name, raw]) => [name, appointmentRecord(raw, timezone)]));
}

function appointmentRecord(raw: string | null, timezone: string): PublishedAppointment {
  if (!raw) return { startDate: null, display: null, timezone };
  const appointment = new Date(raw);
  if (Number.isNaN(appointment.valueOf())) throw new Error(`Invalid appointment datetime: ${raw}`);
  const display = new Intl.DateTimeFormat("en-AU", {
    timeZone: timezone,
    day: "numeric",
    month: "long",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  }).format(appointment).replace(/\b(am|pm)\b/, (marker) => marker.toUpperCase());
  return {
    startDate: raw,
    display,
    timezone,
  };
}

function getNextAppointment(appointments: AppointmentMap, doctor: string, timezone: string): string | null {
  const raw = appointments[doctor];
  if (!raw) throw new Error(`HotDoc response contained no appointment for ${doctor}`);
  const date = new Date(raw);
  if (Number.isNaN(date.valueOf())) throw new Error(`Invalid appointment datetime for ${doctor}: ${raw}`);
  return date.toISOString();
}

async function updateState(state: SentinelState, next: string | null, env: Env, now: Date): Promise<SentinelState> {
  if (!next) return state;
  if (!state.worst) return { ...state, worst: next };
  if (new Date(next) >= new Date(state.worst)) {
    return new Date(next) > new Date(state.worst) ? { ...state, worst: next } : state;
  }

  const repeatHours = Number(env.EMAIL_REPEAT_HOURS || 24);
  const last = state.lastEmail;
  const shouldSend = !last
    || last.newSlot !== next
    || last.previous !== state.worst
    || (now.valueOf() - new Date(last.sentAt).valueOf()) / 3_600_000 >= repeatHours;

  if (shouldSend) {
    const sent = await sendAlert(next, state.worst, env);
    if (!sent) return state;
    return {
      ...state,
      lastEmail: { newSlot: next, previous: state.worst, sentAt: now.toISOString() },
    };
  }
  return state;
}

async function sendAlert(next: string, previous: string, env: Env): Promise<boolean> {
  const newSlot = formatAppointmentTime(next, env.TIMEZONE);
  const previousSlot = formatAppointmentTime(previous, env.TIMEZONE);
  await env.EMAIL.send({
    from: env.SENDER_ADDRESS,
    to: env.ALERT_TO,
    subject: "Earlier appointment available",
    text: `An earlier appointment with Dr Lorna Montgomery is available.\n\nNew slot: ${newSlot}\nPrevious: ${previousSlot}\n\nBook now: ${env.HOTDOC_BOOKING_URL}`,
  });
  return true;
}

async function sendTestEmail(env: Env): Promise<string> {
  const newSlot = formatAppointmentTime("2026-12-06T23:15:00.000Z", env.TIMEZONE);
  const previousSlot = formatAppointmentTime("2026-12-08T04:30:00.000Z", env.TIMEZONE);
  const result = await env.EMAIL.send({
    from: env.SENDER_ADDRESS,
    to: env.ALERT_TO,
    subject: "Appointment Sentinel timezone formatting test",
    text: `This is a formatting test from the Cloudflare Appointment Sentinel Worker.\n\nNew slot: ${newSlot}\nPrevious: ${previousSlot}`,
    html: `<p>This is a formatting test from the Cloudflare Appointment Sentinel Worker.</p><p><strong>New slot:</strong> ${newSlot}<br><strong>Previous:</strong> ${previousSlot}</p>`,
  });
  return result.messageId;
}

async function sendFailureAlert(error: unknown, env: Env): Promise<void> {
  const state = await readState(env.APPOINTMENT_DATA);
  const now = new Date();
  const lastSent = state.lastFailureEmailAt ? new Date(state.lastFailureEmailAt) : null;
  const repeatHours = Number(env.FAILURE_REPEAT_HOURS || 24);
  if (lastSent && (now.valueOf() - lastSent.valueOf()) / 3_600_000 < repeatHours) {
    console.warn("[sentinel] failure alert suppressed by cooldown");
    return;
  }

  const detail = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
  await env.EMAIL.send({
    from: env.SENDER_ADDRESS,
    to: env.ALERT_TO,
    subject: "Appointment Sentinel job failed",
    text: `Appointment Sentinel failed at ${now.toISOString()}.\n\n${detail}\n\nCheck the Worker logs in Cloudflare.`,
    html: `<p>Appointment Sentinel failed at <strong>${now.toISOString()}</strong>.</p><p><code>${escapeHtml(detail)}</code></p><p>Check the Worker logs in Cloudflare.</p>`,
  });
  await writeState(env.APPOINTMENT_DATA, { ...state, lastFailureEmailAt: now.toISOString() });
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;",
  })[character] ?? character);
}

async function readLogs(bucket: R2Bucket): Promise<RunLog[]> {
  const object = await bucket.get(LOGS_KEY);
  if (!object) return [];
  const parsed = await object.json<unknown>();
  return Array.isArray(parsed) ? parsed.filter((entry): entry is RunLog => Boolean(entry && typeof entry === "object")) : [];
}

async function recordRunLog(bucket: R2Bucket, log: RunLog): Promise<void> {
  const logs = await readLogs(bucket);
  logs.unshift(log);
  await bucket.put(LOGS_KEY, JSON.stringify(logs.slice(0, 3)), {
    httpMetadata: { contentType: "application/json; charset=utf-8", cacheControl: "no-store" },
  });
}

async function getLogsPage(env: Env): Promise<Response> {
  const logs = await readLogs(env.APPOINTMENT_DATA);
  const rows = logs.length === 0
    ? `<tr><td colspan="5">No executions recorded yet.</td></tr>`
    : logs.map((log) => {
      const statusClass = log.status === "success" ? "success" : "failure";
      const result = log.status === "success"
        ? `${log.doctors ?? "?"} doctors; ${log.nextAppointment ?? "no appointment"}`
        : escapeHtml(log.error ?? "Unknown error");
      return `<tr><td data-label="Started">${escapeHtml(formatLogTime(log.startedAt, env.TIMEZONE))}</td><td data-label="Trigger">${escapeHtml(log.trigger)}</td><td data-label="Status" class="${statusClass}">${escapeHtml(log.status)}</td><td data-label="Duration">${log.durationMs} ms</td><td data-label="Result">${escapeHtml(result)}</td></tr>`;
    }).join("");

  const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="60"><title>Appointment Sentinel Logs</title>
<style>
*{box-sizing:border-box}body{font:15px system-ui,sans-serif;background:#f6f7f9;color:#17202a;margin:2rem}main{max-width:1100px;margin:auto;background:white;padding:1.5rem 2rem;border-radius:12px;box-shadow:0 2px 12px #0001}h1{margin-top:0}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.7rem;border-bottom:1px solid #e5e7eb;vertical-align:top}th{font-size:.85rem;color:#667085}.success{color:#087f5b;font-weight:600}.failure{color:#c92a2a;font-weight:600}code{white-space:pre-wrap;word-break:break-word}a{color:#175cd3}
@media(max-width:700px){body{margin:.75rem;background:#eef2f6}main{padding:1.1rem;border-radius:16px}h1{font-size:1.8rem;line-height:1.1}p{line-height:1.45}table,thead,tbody,tr,td{display:block}thead{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}tr{margin:1rem 0;border:1px solid #e5e7eb;border-radius:12px;padding:.35rem .75rem;background:#fff}td{display:grid;grid-template-columns:6.2rem minmax(0,1fr);gap:.6rem;padding:.6rem 0;border-bottom:0;overflow-wrap:anywhere}td::before{content:attr(data-label);font-size:.78rem;font-weight:700;color:#667085;text-transform:uppercase;letter-spacing:.03em}.success,.failure{font-weight:700}}
</style></head><body><main><h1>Appointment Sentinel</h1><p>Last three Worker executions. Refreshes every 60 seconds.</p><table><thead><tr><th>Started</th><th>Trigger</th><th>Status</th><th>Duration</th><th>Result</th></tr></thead><tbody>${rows}</tbody></table><p><a href="/logs.json">View JSON</a> · <a href="/appointments.json">View appointments</a></p></main></body></html>`;
  return new Response(html, { headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } });
}

function formatLogTime(value: string, timezone: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat("en-AU", {
    timeZone: timezone,
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    hour12: true,
  }).format(date).replace(/\b(am|pm)\b/, (marker) => marker.toUpperCase());
}

export function formatAppointmentTime(value: string, timezone: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  const display = new Intl.DateTimeFormat("en-AU", {
    timeZone: timezone,
    day: "numeric",
    month: "long",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  }).format(date).replace(/\b(am|pm)\b/, (marker) => marker.toUpperCase());
  return `${display} (${timezone})`;
}

async function readState(bucket: R2Bucket): Promise<SentinelState> {
  const object = await bucket.get(STATE_KEY);
  if (!object) return { worst: null, lastEmail: null, lastFailureEmailAt: null };
  const parsed = (await object.json()) as Partial<SentinelState>;
  return {
    worst: typeof parsed.worst === "string" ? parsed.worst : null,
    lastEmail: parsed.lastEmail && typeof parsed.lastEmail === "object" ? parsed.lastEmail as EmailRecord : null,
    lastFailureEmailAt: typeof parsed.lastFailureEmailAt === "string" ? parsed.lastFailureEmailAt : null,
  };
}

async function writeState(bucket: R2Bucket, state: SentinelState): Promise<void> {
  await bucket.put(STATE_KEY, JSON.stringify(state), {
    httpMetadata: { contentType: "application/json; charset=utf-8", cacheControl: "no-store" },
  });
}

export function filterPublishedAppointments(published: PublishedMap, query: string): PublishedMap {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return published;
  return Object.fromEntries(Object.entries(published).filter(([name]) => name.toLowerCase().includes(normalized)));
}

async function getPublishedAppointments(env: Env, doctor: string | null): Promise<Response> {
  const object = await env.APPOINTMENT_DATA.get(APPOINTMENTS_KEY);
  if (!object) return Response.json({ error: "No appointment snapshot has been published yet" }, { status: 503 });
  if (doctor !== null) {
    const published = await object.json<PublishedMap>();
    const filtered = filterPublishedAppointments(published, doctor);
    if (Object.keys(filtered).length === 0) {
      return Response.json({ error: `No doctor matched: ${doctor}` }, { status: 404 });
    }
    return Response.json(filtered, {
      headers: { "Cache-Control": "no-store", "Access-Control-Allow-Origin": "*" },
    });
  }
  return new Response(await object.arrayBuffer(), {
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "Access-Control-Allow-Origin": "*",
    },
  });
}
