import { render } from "ink-testing-library";
import { describe, expect, it, vi } from "vitest";
import { App, splitSettled } from "./App";
import type { startAgent } from "./agent/connection";
import { type AgentEvent, type ScreenMessage } from "./agent/protocol";

const tick = () => new Promise((resolve) => setTimeout(resolve, 30));

function fakeAgent() {
  const sent: ScreenMessage[] = [];
  let emit: (event: AgentEvent) => void = () => {};
  let close: (code: number | null, stderr: string) => void = () => {};
  const connect: typeof startAgent = (onEvent, onClose) => {
    emit = onEvent;
    close = onClose;
    return { send: (m) => sent.push(m), stop: vi.fn() };
  };

  return { connect, sent, emit: (e: AgentEvent) => emit(e), close: (c: number | null, s: string) => close(c, s) };
}

const ready: AgentEvent = { type: "ready", tools: 27, gated: 14 };
const request: AgentEvent = { type: "approval_request", id: "approval-1", name: "sync_cues_to_serato", arguments: { dry_run: false, note: "[bold]x[/bold]" } };

describe("App", () => {
  it("shows the banner, sends what the user types, and shows the reply", async () => {
    const agent = fakeAgent();
    const { lastFrame, stdin } = render(<App connect={agent.connect} />);
    await tick();
    agent.emit(ready);
    await tick();
    expect(lastFrame()).toContain("27 tools. 14 of them ask you first");

    stdin.write("are my apps in sync?");
    await tick();
    stdin.write("\r");
    await tick();
    expect(agent.sent).toEqual([{ type: "user_message", text: "are my apps in sync?" }]);

    agent.emit({ type: "reply", text: "All **three** are in sync." });
    await tick();
    expect(lastFrame()).toContain("three");
    expect(lastFrame()).not.toContain("**");
  });

  it("shows the approval panel with every argument, unformatted", async () => {
    const agent = fakeAgent();
    const { lastFrame } = render(<App connect={agent.connect} />);
    agent.emit(ready);
    agent.emit(request);
    await tick();
    const frame = lastFrame() ?? "";

    expect(frame).toContain("Tool Approval Required");
    expect(frame).toContain("sync_cues_to_serato");
    expect(frame).toContain("dry_run");
    expect(frame).toContain("[bold]x[/bold]");
  });

  it("sends approve: true only for y", async () => {
    const agent = fakeAgent();
    const { stdin } = render(<App connect={agent.connect} />);
    agent.emit(ready);
    agent.emit(request);
    await tick();
    stdin.write("y");
    await tick();

    expect(agent.sent).toEqual([{ type: "approval_response", id: "approval-1", approve: true }]);
  });

  it("approves with the arrows and Enter, and Enter alone is a no", async () => {
    const agent = fakeAgent();
    const { stdin } = render(<App connect={agent.connect} />);
    agent.emit(ready);
    agent.emit(request);
    await tick();
    stdin.write("\u001b[A"); // up: from No to Yes
    await tick();
    expect(agent.sent).toEqual([]);
    stdin.write("\r");
    await tick();

    expect(agent.sent).toEqual([{ type: "approval_response", id: "approval-1", approve: true }]);
  });

  it("moves back to No with the arrows, then Enter is a no", async () => {
    const agent = fakeAgent();
    const { stdin } = render(<App connect={agent.connect} />);
    agent.emit(ready);
    agent.emit(request);
    await tick();
    stdin.write("\u001b[A");
    await tick();
    stdin.write("\u001b[B");
    await tick();
    stdin.write("\r");
    await tick();

    expect(agent.sent).toEqual([{ type: "approval_response", id: "approval-1", approve: false }]);
  });

  it("quits when the user types exit", async () => {
    const agent = fakeAgent();
    const { stdin } = render(<App connect={agent.connect} />);
    agent.emit(ready);
    await tick();
    stdin.write("exit");
    await tick();
    stdin.write("\r");
    await tick();

    expect(agent.sent).toEqual([{ type: "quit" }]);
  });

  it.each(["n", "\r", "x", "\u001b"])("sends approve: false for %j", async (key) => {
    const agent = fakeAgent();
    const { stdin } = render(<App connect={agent.connect} />);
    agent.emit(ready);
    agent.emit(request);
    await tick();
    stdin.write(key);
    await tick();

    const answers = agent.sent.filter((m) => m.type === "approval_response");
    expect(answers).toHaveLength(1);
    expect(answers[0]).toMatchObject({ approve: false });
  });

  it("does not let typing reach the prompt while a question is open", async () => {
    const agent = fakeAgent();
    const { stdin } = render(<App connect={agent.connect} />);
    agent.emit(ready);
    agent.emit(request);
    await tick();
    stdin.write("n");
    await tick();
    stdin.write("hello\r");
    await tick();

    expect(agent.sent.some((m) => m.type === "user_message")).toBe(false);
  });

  it("says so when the agent stops, with what it printed", async () => {
    const agent = fakeAgent();
    const { lastFrame } = render(<App connect={agent.connect} />);
    agent.emit(ready);
    agent.close(1, "OPENAI_API_KEY missing");
    await tick();

    expect(lastFrame()).toContain("The agent stopped (exit 1).");
    expect(lastFrame()).toContain("OPENAI_API_KEY missing");
  });
});

describe("splitSettled", () => {
  it("keeps everything before the first running tool settled", () => {
    const running = { kind: "tool", id: "a", name: "t", arguments: {}, state: "running", seconds: null, failure: null } as const;
    const { settled, live } = splitSettled([{ kind: "user", text: "hi" }, running, { kind: "note", text: "later" }]);

    expect(settled).toHaveLength(1);
    expect(live).toHaveLength(2);
  });
});

