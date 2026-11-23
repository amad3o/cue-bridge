// Starts the Python agent as a child process and speaks the line protocol with it.

import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { encode, parseEvent, type AgentEvent, type ScreenMessage } from "./protocol";

const here = path.dirname(fileURLToPath(import.meta.url));
export const ROOT = path.resolve(here, "..", "..");

export function pythonPath(): string {
  return process.env.CUEBRIDGE_PYTHON || path.join(ROOT, ".venv", "bin", "python");
}

export type AgentConnection = {
  send: (message: ScreenMessage) => void;
  stop: () => void;
};

/** Start the agent. `onClose` gets what the process left on stderr if it ended. */
export function startAgent(onEvent: (event: AgentEvent) => void, onClose: (code: number | null, stderr: string) => void): AgentConnection {
  const python = pythonPath();

  if (!existsSync(python)) {
    queueMicrotask(() => onClose(null, `No Python found at ${python}. Run \`make setup\` first (or set CUEBRIDGE_PYTHON).`));
    return { send: () => {}, stop: () => {} };
  }

  const child: ChildProcessWithoutNullStreams = spawn(python, ["-m", "cuebridge.agent"], {
    cwd: ROOT,
    env: { ...process.env, PYTHONPATH: path.join(ROOT, "src"), PYTHONUNBUFFERED: "1" },
    stdio: ["pipe", "pipe", "pipe"],
  });

  // stderr must never reach the terminal while the screen is drawing; keep the tail to show if the agent dies.
  let stderr = "";
  child.stderr.on("data", (chunk: Buffer) => {
    stderr = (stderr + chunk.toString()).slice(-4000);
  });

  createInterface({ input: child.stdout }).on("line", (line) => {
    const event = parseEvent(line);
    if (event) onEvent(event);
  });

  child.on("error", (error) => onClose(null, String(error)));
  child.on("close", (code) => onClose(code, stderr.trim()));
  child.stdin.on("error", () => {}); // writing to a dead agent is not an error worth crashing for

  return {
    send: (message) => {
      if (child.stdin.writable) child.stdin.write(encode(message));
    },
    stop: () => {
      child.stdin.end(); // a closed stdin is the agent's cue to stop (and counts as a refusal if it was asking)
      setTimeout(() => child.kill(), 2000).unref();
    },
  };
}
