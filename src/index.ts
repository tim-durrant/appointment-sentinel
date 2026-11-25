const STATE_KEY = "state.json";
const APPOINTMENTS_KEY = "appointments.json";

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
    ctx.waitUntil(runCheck(env));
  },

  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/health") {
      return Response.json({ ok: true, service: "appointment-sentinel" });
    }

    if (request.method === "GET" && url.pathname === "/appointments.json") {
      return getPublishedAppointments(env);
    }

    if (request.method === "POST" && url.pathname === "/run") {
      if (!env.RUN_TOKEN || request.headers.get("Authorization") !== `Bearer ${env.RUN_TOKEN}`) {
        return new Response("Unauthorized", { status: 401 });
      }
      await runCheck(env);
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

export async function runCheck(env: Env, now = new Date()): Promise<void> {
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
  await env.EMAIL.send({
    from: env.SENDER_ADDRESS,
    to: env.ALERT_TO,
    subject: "Earlier appointment available",
    text: `An earlier appointment with Dr Lorna Montgomery is available.\n\nNew slot: ${next}\nPrevious: ${previous}\n\nBook now: ${env.HOTDOC_BOOKING_URL}`,
  });
  return true;
}

async function sendTestEmail(env: Env): Promise<string> {
  const result = await env.EMAIL.send({
    from: env.SENDER_ADDRESS,
    to: env.ALERT_TO,
    subject: "Appointment Sentinel Cloudflare test",
    text: "This is a test email from the Cloudflare Appointment Sentinel Worker.",
    html: "<p>This is a test email from the Cloudflare Appointment Sentinel Worker.</p>",
  });
  return result.messageId;
}

async function readState(bucket: R2Bucket): Promise<SentinelState> {
  const object = await bucket.get(STATE_KEY);
  if (!object) return { worst: null, lastEmail: null };
  const parsed = (await object.json()) as Partial<SentinelState>;
  return {
    worst: typeof parsed.worst === "string" ? parsed.worst : null,
    lastEmail: parsed.lastEmail && typeof parsed.lastEmail === "object" ? parsed.lastEmail as EmailRecord : null,
  };
}

async function writeState(bucket: R2Bucket, state: SentinelState): Promise<void> {
  await bucket.put(STATE_KEY, JSON.stringify(state), {
    httpMetadata: { contentType: "application/json; charset=utf-8", cacheControl: "no-store" },
  });
}

async function getPublishedAppointments(env: Env): Promise<Response> {
  const object = await env.APPOINTMENT_DATA.get(APPOINTMENTS_KEY);
  if (!object) return Response.json({ error: "No appointment snapshot has been published yet" }, { status: 503 });
  return new Response(await object.arrayBuffer(), {
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "Access-Control-Allow-Origin": "*",
    },
  });
}
