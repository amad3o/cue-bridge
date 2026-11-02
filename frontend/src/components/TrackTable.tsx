import { useEffect, useMemo, useState } from "react";
import type { Track } from "../types/cue";

type Props = {
  tracks: Track[];
};

type SortBy = "title" | "artist" | "cueCount";

export default function TrackTable({ tracks }: Props) {
  const [sortBy, setSortBy] = useState<SortBy>("title");
  const [selectedIndex, setSelectedIndex] = useState(0);

  const sortedTracks = useMemo(() => {
    return [...tracks].sort((a, b) => {
      if (sortBy === "title") {
        return (a.trackTitle ?? "").localeCompare(b.trackTitle ?? "");
      }

      if (sortBy === "artist") {
        return (a.artist ?? "").localeCompare(b.artist ?? "");
      }

      return b.cues.length - a.cues.length;
    });
  }, [tracks, sortBy]);

  const safeSelectedIndex =
    sortedTracks.length > 0
      ? Math.min(selectedIndex, sortedTracks.length - 1)
      : 0;

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (sortedTracks.length === 0) return;

      if (event.key === "ArrowDown") {
        event.preventDefault();

        setSelectedIndex((prev) => Math.min(prev + 1, sortedTracks.length - 1));
      }

      if (event.key === "ArrowUp") {
        event.preventDefault();

        setSelectedIndex((prev) => Math.max(prev - 1, 0));
      }
    };

    window.addEventListener("keydown", handleKeyDown);

    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [sortedTracks.length]);

  return (
    <div style={{ marginTop: "2rem" }}>
      <h2>Parsed Tracks</h2>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "60px 1.5fr 1fr 90px 2fr",
          padding: "0.75rem 1rem",
          background: "#0f1014",
          color: "#aaa",
          fontWeight: "bold",
        }}
      >
        <div>Wave</div>

        <div onClick={() => setSortBy("title")} style={{ cursor: "pointer" }}>
          Track {sortBy === "title" ? "▲" : ""}
        </div>

        <div onClick={() => setSortBy("artist")} style={{ cursor: "pointer" }}>
          Artist {sortBy === "artist" ? "▲" : ""}
        </div>

        <div
          onClick={() => setSortBy("cueCount")}
          style={{ cursor: "pointer" }}
        >
          Cues {sortBy === "cueCount" ? "▲" : ""}
        </div>

        <div>Cue Times</div>
      </div>

      {sortedTracks.map((track, index) => {
        const isSelected = safeSelectedIndex === index;

        return (
          <div
            key={track.trackId}
            onClick={() => setSelectedIndex(index)}
            style={{
              display: "grid",
              gridTemplateColumns: "60px 1.5fr 1fr 90px 2fr",
              alignItems: "center",
              gap: "1rem",
              padding: "0.75rem 1rem",
              borderBottom: "1px solid #2a2a2a",
              background: isSelected ? "#252a36" : "#14151a",
              cursor: "pointer",
            }}
          >
            <div style={{ color: "#888" }}>|||</div>

            <div
              style={{
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
                minWidth: 0,
              }}
            >
              {track.trackTitle || "Untitled"}
            </div>

            <div
              style={{
                color: "#999",
                fontSize: "0.85rem",
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
                minWidth: 0,
              }}
            >
              {track.artist || "Unknown Artist"}
            </div>

            <div style={{ color: "#aaa" }}>{track.cues.length}</div>

            <div
              style={{
                color: "#bbb",
                fontSize: "0.8rem",
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
              }}
            >
              {track.cues.length > 0
                ? track.cues
                    .map((cue) => {
                      const rawTime = cue.timeMs;

                      if (
                        typeof rawTime !== "number" ||
                        Number.isNaN(rawTime)
                      ) {
                        return "--:--";
                      }

                      const minutes = Math.floor(rawTime / 60000);
                      const seconds = Math.floor((rawTime % 60000) / 1000);

                      return `${minutes.toString().padStart(2, "0")}:${seconds
                        .toString()
                        .padStart(2, "0")}`;
                    })
                    .join(" • ")
                : "No cues"}
            </div>
          </div>
        );
      })}
    </div>
  );
}
