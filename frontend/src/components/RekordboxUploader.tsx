import React, { useState } from "react";
import { uploadRekordboxXml } from "../services/rekordboxApi";
import type { Track } from "../types/cue";
import TrackTable from "./TrackTable";

export default function RekordboxUploader() {
  const [file, setFile] = useState<File | null>(null);
  const [tracks, setTracks] = useState<Track[]>([]);
  const [loading, setLoading] = useState(false);
  const [uploadSummary, setUploadSummary] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [showOnlyWithCues, setShowOnlyWithCues] = useState(false);

  const uploadFile = async () => {
    if (!file) return;

    setError(null);

    try {
      setLoading(true);

      const data = await uploadRekordboxXml(file);

      setTracks(data.tracks ?? []);

      setUploadSummary(
        `Parsed ${data.tracksFound} tracks from ${data.filename}`
      );
    } catch (error) {
      console.error("Failed to upload Rekordbox XML:", error);

      setError(
        "Failed to upload Rekordbox XML. Make sure your backend is running."
      );

      setUploadSummary(null);

      setTracks([]);
    } finally {
      setLoading(false);
    }
  };

  const handleDrop = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();

    const droppedFile = event.dataTransfer.files?.[0];

    if (droppedFile && droppedFile.name.endsWith(".xml")) {
      setFile(droppedFile);
    }
  };

  const filteredTracks = tracks.filter((track) => {
    const matchesSearch = `${track.trackTitle ?? ""} ${track.artist ?? ""}`
      .toLowerCase()
      .includes(searchQuery.toLowerCase());

    const matchesCueFilter = showOnlyWithCues ? track.cues.length > 0 : true;

    return matchesSearch && matchesCueFilter;
  });

  return (
    <div
      style={{
        minHeight: "100vh",
        padding: "2rem",
        overflowY: "auto",
      }}
    >
      <h1>CueBridge</h1>

      <div
        onDragOver={(event) => event.preventDefault()}
        onDrop={handleDrop}
        style={{
          border: "2px dashed #999",
          borderRadius: "12px",
          padding: "2rem",
          marginTop: "1rem",
          textAlign: "center",
        }}
      >
        <p>Drag and drop your Rekordbox XML here</p>

        <p>or</p>

        <input
          type="file"
          accept=".xml"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />

        <br />

        <button onClick={uploadFile} disabled={!file || loading}>
          {loading ? "Uploading..." : "Upload XML"}
        </button>

        {error && <p style={{ color: "red" }}>{error}</p>}

        {file && <p>Selected: {file.name}</p>}
      </div>
      {uploadSummary && <p style={{ color: "lightgreen" }}>{uploadSummary}</p>}

      <input
        type="text"
        placeholder="Search tracks..."
        value={searchQuery}
        onChange={(e) => setSearchQuery(e.target.value)}
        style={{
          marginTop: "2rem",
          width: "100%",
          padding: "0.75rem",
          borderRadius: "8px",
          border: "1px solid #333",
          background: "#14151a",
          color: "white",
        }}
      />
      <label style={{ display: "block", marginTop: "1rem" }}>
        <input
          type="checkbox"
          checked={showOnlyWithCues}
          onChange={(e) => setShowOnlyWithCues(e.target.checked)}
        />{" "}
        Show only tracks with cues
      </label>

      {tracks.length > 0 && (
        <p style={{ marginTop: "1rem", color: "#aaa" }}>
          Showing {filteredTracks.length} of {tracks.length} tracks
        </p>
      )}

      {tracks.length > 0 && <TrackTable tracks={filteredTracks} />}
    </div>
  );
}
