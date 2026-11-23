import type { Args } from "./protocol";

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
