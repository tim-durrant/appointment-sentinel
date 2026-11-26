import { describe, expect, it } from "vitest";
import { buildPublishedAppointments, filterPublishedAppointments, formatAppointmentTime } from "../src/index.js";

describe("published appointment records", () => {
  it("preserves doctors without an available appointment", () => {
    expect(buildPublishedAppointments({
      "Lorna Montgomery": "2026-10-12T11:45:00+10:00",
      "Other Doctor": null,
    }, "Australia/Brisbane")).toEqual({
      "Lorna Montgomery": {
        startDate: "2026-10-12T11:45:00+10:00",
        display: "12 October 2026 at 11:45 AM",
        timezone: "Australia/Brisbane",
      },
      "Other Doctor": {
        startDate: null,
        display: null,
        timezone: "Australia/Brisbane",
      },
    });
  });
});

describe("appointment email times", () => {
  it("formats UTC values in the configured Brisbane timezone", () => {
    expect(formatAppointmentTime("2026-12-06T23:15:00.000Z", "Australia/Brisbane"))
      .toBe("7 December 2026 at 9:15 AM (Australia/Brisbane)");
  });
});

describe("appointment filtering", () => {
  it("matches a doctor case-insensitively by partial name", () => {
    const published = buildPublishedAppointments({
      "Lorna Montgomery": "2026-10-12T11:45:00+10:00",
      "Other Doctor": null,
    }, "Australia/Brisbane");

    expect(Object.keys(filterPublishedAppointments(published, "montgomery")))
      .toEqual(["Lorna Montgomery"]);
  });
});
