import { Box, Text, useInput } from "ink";
import { useState } from "react";

type Props = { onSubmit: (text: string) => void; onQuit: () => void; disabled?: boolean };

/** A one-line prompt with history (up and down). Ctrl-D on an empty line quits (Ctrl-C is handled by the app). */
export function TextInput({ onSubmit, onQuit, disabled = false }: Props) {
  const [value, setValue] = useState("");
  const [history, setHistory] = useState<string[]>([]);
  const [position, setPosition] = useState<number | null>(null);

  useInput(
    (input, key) => {
      if (key.ctrl && input === "d") return value === "" ? onQuit() : undefined;

      if (key.return) {
        const text = value.trim();
        if (!text) return;
        setHistory((h) => [...h, text]);
        setPosition(null);
        setValue("");
        return onSubmit(text);
      }

      if (key.upArrow && history.length) {
        const next = position === null ? history.length - 1 : Math.max(0, position - 1);
        setPosition(next);
        return setValue(history[next]);
      }

      if (key.downArrow && position !== null) {
        const next = position + 1;
        if (next >= history.length) {
          setPosition(null);
          return setValue("");
        }
        setPosition(next);
        return setValue(history[next]);
      }

      if (key.backspace || key.delete) return setValue((v) => v.slice(0, -1));
      if (key.ctrl && input === "u") return setValue("");
      if (key.ctrl || key.meta || key.escape || key.tab || key.upArrow || key.downArrow || key.leftArrow || key.rightArrow) return;

      if (input) setValue((v) => v + input.replace(/[\r\n]+/g, " "));
    },
    { isActive: !disabled },
  );

  return (
    <Box>
      <Text bold color="cyan">
        you&gt;{" "}
      </Text>
      <Text dimColor={disabled}>{value}</Text>
      {!disabled && <Text inverse> </Text>}
    </Box>
  );
}
