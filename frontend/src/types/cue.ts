export type CueType = "hotcue" | "memory" | "loop";

export type NormalizedCue = {
  id: string;
  source: "rekordbox" | "serato";
  trackTitle: string | null;
  artist: string | null;
  filePath: string | null;
  // Hot cue / hot loop slot. null for memory cues and memory loops.
  cueIndex: number | null;
  cueName?: string;
  timeMs: number;
  // Loop end time. null for anything that isn't a loop.
  endMs: number | null;
  // Hex color like "#28e214", or null when the source has no color.
  color: string | null;
  cueType: CueType;
};

export type Track = {
  trackId: string;
  // null when the source file has no title or artist (not the text "Unknown").
  trackTitle: string | null;
  artist: string | null;
  filePath: string | null;
  cues: NormalizedCue[];
};
