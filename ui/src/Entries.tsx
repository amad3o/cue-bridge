import { Box, Text } from "ink";
import { argumentSummary, short, took } from "./format";
import { renderMarkdown } from "./markdown";
import type { Approval, Entry } from "./state";

export function EntryView({ entry, width }: { entry: Entry; width: number }) {
  switch (entry.kind) {
    case "banner":
      return (
        <Box borderStyle="round" borderColor="cyan" flexDirection="column" paddingX={1}>
          <Text bold>CueBridge agent</Text>
          <Text>
            {entry.tools} tools. {entry.gated} of them ask you first before a real change; dry runs and reads do not.
          </Text>
          <Text dimColor>Ctrl-D on an empty line (or Ctrl-C) quits. Up and down recall earlier messages.</Text>
        </Box>
      );
    case "user":
      return (
        <Box marginTop={1}>
          <Text bold color="cyan">
            you&gt;{" "}
          </Text>
          <Text>{entry.text}</Text>
        </Box>
      );
    case "tool":
      return <ToolLine entry={entry} />;
    case "reply":
      return (
        <Box marginTop={1}>
          <Text>{entry.text ? renderMarkdown(entry.text, Math.max(40, width - 2)) : ""}</Text>
          {!entry.text && <Text dimColor>(no reply)</Text>}
        </Box>
      );
    case "error":
      return (
        <Box borderStyle="round" borderColor="red" flexDirection="column" paddingX={1} marginTop={1}>
          <Text bold color="red">
            Something went wrong
          </Text>
          <Text>{entry.message}</Text>
        </Box>
      );
    case "note":
      return <Text dimColor>{entry.text}</Text>;
  }
}

function ToolLine({ entry }: { entry: Extract<Entry, { kind: "tool" }> }) {
  if (entry.state === "running")
    return (
      <Text>
        <Text dimColor>{"  → "}</Text>
        <Text bold>{entry.name}</Text>
        <Text dimColor>({argumentSummary(entry.arguments)})</Text>
      </Text>
    );

  if (entry.state === "done")
    return (
      <Text>
        <Text color="green">{"  ✓ "}</Text>
        {entry.name}
        <Text dimColor>{took(entry.seconds)}</Text>
      </Text>
    );

  return (
    <Text>
      <Text color="red">{"  ✗ "}</Text>
      {entry.name}
      <Text dimColor>{took(entry.seconds)}</Text>
      <Text color="red"> {short(entry.failure ?? "", 110).replace(/^'|'$/g, "")}</Text>
    </Text>
  );
}

export function ApprovalPanel({ approval }: { approval: Approval }) {
  return (
    <Box borderStyle="round" borderColor="red" flexDirection="column" paddingX={1} marginTop={1}>
      <Text bold color="red">
        Approval needed
      </Text>
      <Text bold color="red">
        This will really change something.
      </Text>
      <Text>
        <Text dimColor>Tool: </Text>
        <Text bold>{approval.name}</Text>
      </Text>
      <Box flexDirection="column" marginTop={1}>
        {approval.arguments === null ? (
          <Text color="yellow">(the arguments could not be read)</Text>
        ) : (
          Object.entries(approval.arguments).map(([key, value]) => (
            <Text key={key}>
              <Text dimColor>{key.padEnd(18)}</Text>
              {short(value, 100)}
            </Text>
          ))
        )}
      </Box>
      <Box marginTop={1}>
        <Text bold>
          Go ahead? <Text color="green">y</Text> = yes, anything else = no
        </Text>
      </Box>
    </Box>
  );
}
