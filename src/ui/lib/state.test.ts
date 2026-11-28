import { describe, expect, it } from "vitest";
import { initial, reduce, type State } from "./state";
import type { AgentEvent } from "../agent/protocol";

const feed = (events: AgentEvent[], from: State = initial) => events.reduce((s, event) => reduce(s, { type: "event", event }), from);

describe("reduce", () => {
  it("shows a banner when the agent is ready", () => {
    const s = feed([{ type: "ready", tools: 27, gated: 14 }]);
    expect(s.ready).toBe(true);
    expect(s.entries).toEqual([{ kind: "banner", tools: 27, gated: 14 }]);
  });

  it("turns a running tool line into a finished one, keeping its arguments", () => {
    const s = feed([
      { type: "tool_start", id: "c1", name: "compare_libraries", arguments: { limit: 5 } },
      { type: "tool_end", id: "c1", name: "compare_libraries", seconds: 1.2, failure: null },
    ]);
    expect(s.entries).toEqual([{ kind: "tool", id: "c1", name: "compare_libraries", arguments: { limit: 5 }, state: "done", seconds: 1.2, failure: null }]);
  });

  it("marks a failed tool with its reason", () => {
    const s = feed([
      { type: "tool_start", id: "c1", name: "x", arguments: {} },
      { type: "tool_end", id: "c1", name: "x", seconds: 0.1, failure: "no file" },
    ]);
    expect(s.entries[0]).toMatchObject({ state: "failed", failure: "no file" });
  });

  it("times two calls at once separately", () => {
    const s = feed([
      { type: "tool_start", id: "a", name: "one", arguments: {} },
      { type: "tool_start", id: "b", name: "two", arguments: {} },
      { type: "tool_end", id: "b", name: "two", seconds: 2, failure: null },
    ]);
    expect(s.entries.map((e) => e.kind === "tool" && e.state)).toEqual(["running", "done"]);
  });

  it("still shows a finish that had no start", () => {
    const s = feed([{ type: "tool_end", id: "z", name: "x", seconds: null, failure: null }]);
    expect(s.entries).toHaveLength(1);
  });

  it("holds an approval request until it is answered", () => {
    let s = feed([{ type: "approval_request", id: "approval-1", name: "sync_cues_to_serato", arguments: { dry_run: false }, preview: ["2 files"] }]);
    expect(s.approval).toEqual({ id: "approval-1", name: "sync_cues_to_serato", arguments: { dry_run: false }, preview: ["2 files"] });
    s = reduce(s, { type: "answered" });
    expect(s.approval).toBeNull();
  });

  it("stops the spinner on an error and keeps going", () => {
    const s = feed([{ type: "thinking", on: true }, { type: "error", message: "boom" }]);
    expect(s.thinking).toBe(false);
    expect(s.entries[0]).toEqual({ kind: "error", message: "boom" });
    expect(s.closed).toBe(false);
  });

  it("adds the token counts as a line of their own", () => {
    const count = { calls: 1, input: 100, cached: 0, output: 7 };
    const s = feed([{ type: "reply", text: "ok" }, { type: "usage", turn: count, session: count, last_input: 100 }]);
    expect(s.entries.at(-1)).toEqual({ kind: "usage", turn: count, session: count, lastInput: 100 });
  });

  it("closing drops any open question and says so", () => {
    let s = feed([{ type: "approval_request", id: "approval-1", name: "x", arguments: null, preview: [] }]);
    s = reduce(s, { type: "closed", note: "The agent stopped." });
    expect(s).toMatchObject({ closed: true, approval: null, thinking: false });
    expect(s.entries.at(-1)).toEqual({ kind: "note", text: "The agent stopped." });
  });
});
