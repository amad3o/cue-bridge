import { describe, expect, it } from "vitest";
import { compact, usageSummary } from "./format";

describe("compact", () => {
  it.each([
    [0, "0"],
    [999, "999"],
    [1000, "1.0k"],
    [12_449, "12.4k"],
    [999_949, "999.9k"],
    [999_950, "1.00M"],
    [1_254_000, "1.25M"],
  ])("writes %i as %s", (count, text) => {
    expect(compact(count)).toBe(text);
  });
});

describe("usageSummary", () => {
  const session = { calls: 5, input: 44_000, cached: 20_000, output: 1_200 };

  it("shows this message, the latest call and the session", () => {
    expect(usageSummary({ calls: 3, input: 18_600, cached: 8_100, output: 310 }, session, 6_200)).toBe(
      "Tokens: 18.6k in (8.1k cached), 310 out, 3 model calls · last call sent 6.2k · session 45.2k",
    );
  });

  it("leaves out an empty cache and says one call in the singular", () => {
    expect(usageSummary({ calls: 1, input: 500, cached: 0, output: 20 }, session, 500)).toContain("Tokens: 500 in, 20 out, 1 model call ·");
  });
});
