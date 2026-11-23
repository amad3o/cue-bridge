import { describe, expect, it } from "vitest";
import { encode, parseEvent } from "./protocol";

describe("parseEvent", () => {
  it("reads a known event", () => {
    expect(parseEvent('{"type":"reply","text":"hi"}')).toEqual({ type: "reply", text: "hi" });
  });

  it.each(["", "nope", "[1]", "5", "null", "{}", '{"type":5}', '{"type":"mystery"}'])("ignores %j", (line) => {
    expect(parseEvent(line)).toBeNull();
  });
});

describe("encode", () => {
  it("is one JSON line", () => {
    const line = encode({ type: "user_message", text: "a\nb" });
    expect(line.endsWith("\n")).toBe(true);
    expect(line.trimEnd().includes("\n")).toBe(false);
    expect(JSON.parse(line)).toEqual({ type: "user_message", text: "a\nb" });
  });

  it("sends approve as a real boolean", () => {
    expect(JSON.parse(encode({ type: "approval_response", id: "approval-1", approve: true })).approve).toBe(true);
  });
});
