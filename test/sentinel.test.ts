import { describe, expect, it } from "vitest";
import { buildPublishedAppointments } from "../src/index.js";

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
