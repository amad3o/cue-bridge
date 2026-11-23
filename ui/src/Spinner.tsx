import { Text } from "ink";
import { useEffect, useState } from "react";

const FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

function useFrame(): string {
  const [frame, setFrame] = useState(0);

  useEffect(() => {
    const timer = setInterval(() => setFrame((f) => (f + 1) % FRAMES.length), 80);
    return () => clearInterval(timer);
  }, []);

  return FRAMES[frame];
}

/** Just the cyan dots, to sit inside a line. */
export function Dots() {
  return <Text color="cyan">{useFrame()}</Text>;
}

export function Spinner({ label = "Thinking..." }: { label?: string }) {
  return (
    <Text>
      <Dots /> <Text dimColor>{label}</Text>
    </Text>
  );
}
