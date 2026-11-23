import { Box, Static, Text, useApp, useInput, useStdout } from "ink";
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { startAgent, type AgentConnection } from "./agent/connection";
import { ApprovalPanel, EntryView } from "./components/Entries";
import { Spinner } from "./components/Spinner";
import { initial, reduce, type Entry } from "./lib/state";
import { TextInput } from "./components/TextInput";

/** Entries before the first running tool never change again, so they can be printed once and left alone. */
export function splitSettled(entries: Entry[]): { settled: Entry[]; live: Entry[] } {
  const firstRunning = entries.findIndex((e) => e.kind === "tool" && e.state === "running");
  const cut = firstRunning === -1 ? entries.length : firstRunning;

  return { settled: entries.slice(0, cut), live: entries.slice(cut) };
}

export function App({ connect = startAgent }: { connect?: typeof startAgent }) {
  const { exit } = useApp();
  const { stdout } = useStdout();
  const [state, dispatch] = useReducer(reduce, initial);
  const agent = useRef<AgentConnection | null>(null);
  const quitting = useRef(false);
  const [selected, setSelected] = useState<0 | 1>(1); // 0 = Yes, 1 = No: every question starts on No

  const quit = useCallback(() => {
    quitting.current = true;
    agent.current?.send({ type: "quit" });
    agent.current?.stop();
    exit();
  }, [exit]);

  useEffect(() => {
    agent.current = connect(
      (event) => dispatch({ type: "event", event }),
      (code, stderr) => {
        if (quitting.current) return;
        dispatch({ type: "closed", note: `The agent stopped${code === null ? "" : ` (exit ${code})`}.${stderr ? `\n${stderr}` : ""}` });
      },
    );

    return () => agent.current?.stop();
  }, [connect]);

  useEffect(() => setSelected(1), [state.approval?.id]);

  // Ctrl-C always leaves, whatever the agent is doing. While a question is open, the arrows only move the choice;
  // Enter takes the choice (which starts on No), "y" is a yes, and any other key is a no. The agent treats a
  // missing answer as a no too.
  useInput((input, key) => {
    if (key.ctrl && input === "c") return quit();
    if (!state.approval) return;
    if (key.upArrow || key.downArrow) return setSelected((s) => (s === 0 ? 1 : 0));

    const approve = key.return ? selected === 0 : input === "y" || input === "Y";
    agent.current?.send({ type: "approval_response", id: state.approval.id, approve });
    dispatch({ type: "answered" });
  });

  const submit = (text: string) => {
    if (["exit", "quit"].includes(text.toLowerCase())) return quit();
    dispatch({ type: "user", text });
    agent.current?.send({ type: "user_message", text });
  };

  const width = stdout?.columns ?? 100;
  const { settled, live } = splitSettled(state.entries);
  const busy = state.thinking || state.approval !== null || !state.ready;

  return (
    <Box flexDirection="column">
      <Static items={settled}>{(entry, index) => <EntryView key={index} entry={entry} width={width} />}</Static>
      {live.map((entry, index) => (
        <EntryView key={index} entry={entry} width={width} />
      ))}
      {state.approval && <ApprovalPanel approval={state.approval} selected={selected} />}
      {state.thinking && !state.approval && <Spinner />}
      {!state.ready && !state.closed && <Spinner label="Starting the agent and the CueBridge tools..." />}
      {state.closed ? (
        <Text dimColor>Press Ctrl-C to leave.</Text>
      ) : (
        <Box marginTop={1}>
          <TextInput onSubmit={submit} onQuit={quit} disabled={busy} />
        </Box>
      )}
    </Box>
  );
}
