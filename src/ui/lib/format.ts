import type { Args, TokenCount } from "../agent/protocol";

/** Python-ish repr of a JSON value, cut to `limit` characters. */
export function short(value: unknown, limit = 70): string {
  const text = typeof value === "string" ? `'${value}'` : value === true ? "True" : value === false ? "False" : value === null ? "None" : JSON.stringify(value);

  return text.length <= limit ? text : text.slice(0, limit - 1) + "…";
}

export function argumentSummary(args: Args): string {
  if (args === null) return "(arguments could not be read)";

  return Object.entries(args)
    .map(([key, value]) => `${key}=${short(value, 40)}`)
    .join(", ");
}

export function took(seconds: number | null): string {
  return seconds === null ? "" : ` ${seconds.toFixed(1)}s`;
}

/** A token count at a glance: 950, 12.4k, 1.25M. */
export function compact(count: number): string {
  if (count < 1000) return String(count);

  return count < 999_950 ? `${(count / 1000).toFixed(1)}k` : `${(count / 1_000_000).toFixed(2)}M`;
}

/** The line under a reply. The session figure is everything sent and written so far. */
export function usageSummary(turn: TokenCount, session: TokenCount, lastInput: number): string {
  const cached = turn.cached > 0 ? ` (${compact(turn.cached)} cached)` : "";
  const calls = `${turn.calls} model call${turn.calls === 1 ? "" : "s"}`;

  return `Tokens: ${compact(turn.input)} in${cached}, ${compact(turn.output)} out, ${calls} · last call sent ${compact(lastInput)} · session ${compact(session.input + session.output)}`;
}
