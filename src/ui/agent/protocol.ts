// The conversation with the Python agent: one JSON object per line. The full description is in
// src/cuebridge/agent/protocol.py. The screen only displays things and sends back what the user typed.

export type Args = Record<string, unknown> | null;

export type TokenCount = { calls: number; input: number; cached: number; output: number };

export type AgentEvent =
  | { type: "ready"; tools: number; gated: number }
  | { type: "thinking"; on: boolean }
  | { type: "tool_start"; id: string; name: string; arguments: Args }
  | { type: "tool_end"; id: string; name: string; seconds: number | null; failure: string | null }
  | { type: "approval_request"; id: string; name: string; arguments: Args; preview: string[] }
  | { type: "reply"; text: string }
  | { type: "error"; message: string }
  | { type: "usage"; turn: TokenCount; session: TokenCount; last_input: number };

export type ScreenMessage =
  | { type: "user_message"; text: string }
  | { type: "approval_response"; id: string; approve: boolean }
  | { type: "quit" };

const KINDS = new Set(["ready", "thinking", "tool_start", "tool_end", "approval_request", "reply", "error", "usage"]);

/** One line from the agent as an event, or null if it is not a JSON object with a known type. */
export function parseEvent(line: string): AgentEvent | null {
  let value: unknown;

  try {
    value = JSON.parse(line);
  } catch {
    return null;
  }

  if (typeof value !== "object" || value === null) return null;
  const kind = (value as { type?: unknown }).type;

  return typeof kind === "string" && KINDS.has(kind) ? (value as AgentEvent) : null;
}

export function encode(message: ScreenMessage): string {
  return JSON.stringify(message) + "\n";
}
