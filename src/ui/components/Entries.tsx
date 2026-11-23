import { Box, Text } from "ink";
import { argumentSummary, short, took } from "../lib/format";
import { renderMarkdown } from "../lib/markdown";
import { Dots } from "./Spinner";
import type { Approval, Entry } from "../lib/state";

export function EntryView({ entry, width }: { entry: Entry; width: number }) {
  switch (entry.kind) {
    case "banner":
      return (
        <Box flexDirection="column" marginBottom={1}>
          <Text>
            <Text bold color="magenta">
              🤖 CueBridge Agent
            </Text>
            <Text dimColor> (type "exit" to quit)</Text>
          </Text>
          <Text dimColor>
            {entry.tools} tools. {entry.gated} of them ask you first before a real change; dry runs and reads do not.
          </Text>
        </Box>
      );
    case "user":
      return (
        <Box flexDirection="column" marginTop={1}>
          <Text bold color="blue">
            › You
          </Text>
          <Box marginLeft={2}>
            <Text>{entry.text}</Text>
          </Box>
        </Box>
      );
    case "tool":
      return <ToolLine entry={entry} />;
    case "reply":
      return (
        <Box flexDirection="column" marginTop={1}>
          <Text bold color="green">
            › Assistant
          </Text>
          <Box marginLeft={2}>
            <Text>{entry.text ? renderMarkdown(entry.text, Math.max(40, width - 4)) : ""}</Text>
            {!entry.text && <Text dimColor>(no reply)</Text>}
          </Box>
        </Box>
      );
    case "error":
      return (
        <Box flexDirection="column" marginTop={1}>
          <Text bold color="red">
            › Something went wrong
          </Text>
          <Box marginLeft={2}>
            <Text>{entry.message}</Text>
          </Box>
        </Box>
      );
    case "note":
      return <Text dimColor>{entry.text}</Text>;
  }
}

function ToolLine({ entry }: { entry: Extract<Entry, { kind: "tool" }> }) {
  const failed = entry.state === "failed";

  return (
    <Box flexDirection="column" marginLeft={2}>
      <Box>
        <Text color={failed ? "red" : "yellow"}>⚡ </Text>
        <Text color={failed ? "red" : "yellow"} bold>
          {entry.name}
        </Text>
        {entry.state === "running" && (
          <Text>
            {" "}
            <Dots />
          </Text>
        )}
        {entry.state === "done" && <Text color="green"> ✓</Text>}
        {failed && <Text color="red"> ✗</Text>}
        {entry.state !== "running" && <Text dimColor>{took(entry.seconds)}</Text>}
      </Box>
      <Box marginLeft={2}>
        <Text dimColor>→ {short(argumentSummary(entry.arguments), 100).replace(/^'|'$/g, "")}</Text>
      </Box>
      {failed && (
        <Box marginLeft={2}>
          <Text color="red">{short(entry.failure ?? "", 110).replace(/^'|'$/g, "")}</Text>
        </Box>
      )}
    </Box>
  );
}

/** `selected` is 0 for Yes and 1 for No; it starts on No, so Enter alone never approves a real change. */
export function ApprovalPanel({ approval, selected }: { approval: Approval; selected: 0 | 1 }) {
  const options = ["Yes", "No"];

  return (
    <Box flexDirection="column" marginTop={1}>
      <Text color="yellow" bold>
        Tool Approval Required
      </Text>
      <Box marginLeft={2} flexDirection="column">
        <Text>
          <Text color="cyan" bold>
            {approval.name}
          </Text>
          <Text dimColor> would really change something</Text>
        </Text>
        <Box marginLeft={2} flexDirection="column">
          {approval.arguments === null ? (
            <Text color="yellow">(the arguments could not be read)</Text>
          ) : (
            Object.entries(approval.arguments).map(([key, value]) => (
              <Text key={key} dimColor>
                {key.padEnd(18)}
                {short(value, 100)}
              </Text>
            ))
          )}
        </Box>
      </Box>
      <Box marginTop={1} marginLeft={2} flexDirection="row" gap={2}>
        {options.map((option, index) => (
          <Text key={option} color={selected === index ? "green" : "gray"} bold={selected === index}>
            {selected === index ? "› " : "  "}
            {option}
          </Text>
        ))}
      </Box>
      <Box marginLeft={2}>
        <Text dimColor>↑↓ and Enter to choose, or y for yes. Any other key is no.</Text>
      </Box>
    </Box>
  );
}
