import type { NormalizedCue } from "../types/cue";
import { formatCueTime } from "../utils/time";
type Props = {
  cue: NormalizedCue;
};

export default function CueCard({ cue }: Props) {
  return (
    <div
      style={{
        border: "1px solid #ccc",
        borderLeft: cue.color ? `6px solid ${cue.color}` : "1px solid #ccc",
        padding: "0.75rem",
        marginTop: "0.5rem",
      }}
    >
      <strong>{cue.cueName ?? "Unnamed cue"}</strong>
      <p>
        Time: {formatCueTime(cue.timeMs)}
        {cue.endMs !== null && ` → ${formatCueTime(cue.endMs)}`}
      </p>
      <p>Type: {cue.cueType}</p>
      <p>Slot: {cue.cueIndex !== null ? cue.cueIndex + 1 : "Memory"}</p>
    </div>
  );
}
