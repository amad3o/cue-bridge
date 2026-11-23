import { Marked } from "marked";
import { markedTerminal } from "marked-terminal";

/** Markdown as styled terminal text (ANSI), without the trailing blank lines. */
export function renderMarkdown(text: string, width = 100): string {
  const marked = new Marked(markedTerminal({ width, reflowText: true, tab: 2 }) as never);
  const out = marked.parse(text, { async: false }) as string;

  return out.replace(/\s+$/, "");
}
