// What the screen shows, as a pure function of the agent's events (so it can be tested without a terminal).

import type { AgentEvent, Args } from "../agent/protocol";

export type Entry =
  | { kind: "banner"; tools: number; gated: number }
  | { kind: "user"; text: string }
  | { kind: "tool"; id: string; name: string; arguments: Args; state: "running" | "done" | "failed"; seconds: number | null; failure: string | null }
  | { kind: "reply"; text: string }
  | { kind: "error"; message: string }
  | { kind: "note"; text: string };

export type Approval = { id: string; name: string; arguments: Args; preview: string[] };

export type State = {
  entries: Entry[];
  ready: boolean;
  thinking: boolean;
  approval: Approval | null;
  closed: boolean;
};

export const initial: State = { entries: [], ready: false, thinking: false, approval: null, closed: false };

export type Action = { type: "event"; event: AgentEvent } | { type: "user"; text: string } | { type: "answered" } | { type: "closed"; note: string };

export function reduce(state: State, action: Action): State {
  switch (action.type) {
    case "user":
      return { ...state, entries: [...state.entries, { kind: "user", text: action.text }] };
    case "answered":
      return { ...state, approval: null };
    case "closed":
      return { ...state, closed: true, thinking: false, approval: null, entries: [...state.entries, { kind: "note", text: action.note }] };
    case "event":
      return applyEvent(state, action.event);
  }
}

function applyEvent(state: State, event: AgentEvent): State {
  switch (event.type) {
    case "ready":
      return { ...state, ready: true, entries: [...state.entries, { kind: "banner", tools: event.tools, gated: event.gated }] };
    case "thinking":
      return { ...state, thinking: event.on };
    case "tool_start":
      return {
        ...state,
        entries: [...state.entries, { kind: "tool", id: event.id, name: event.name, arguments: event.arguments, state: "running", seconds: null, failure: null }],
      };
    case "tool_end": {
      const index = state.entries.findIndex((e) => e.kind === "tool" && e.id === event.id && e.state === "running");
      const previous = index === -1 ? null : (state.entries[index] as Extract<Entry, { kind: "tool" }>);
      const finished: Entry = {
        kind: "tool",
        id: event.id,
        name: event.name,
        arguments: previous?.arguments ?? null,
        state: event.failure === null ? "done" : "failed",
        seconds: event.seconds,
        failure: event.failure,
      };
      const entries = [...state.entries];

      if (index === -1) entries.push(finished);
      else entries[index] = finished;

      return { ...state, entries };
    }
    case "approval_request":
      return { ...state, approval: { id: event.id, name: event.name, arguments: event.arguments, preview: event.preview } };
    case "reply":
      return { ...state, entries: [...state.entries, { kind: "reply", text: event.text }] };
    case "error":
      return { ...state, thinking: false, entries: [...state.entries, { kind: "error", message: event.message }] };
  }
}
