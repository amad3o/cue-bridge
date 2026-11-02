def parse_serato_metadata(file_content: bytes):
    preview = file_content[:1000]

    decoded_preview = preview.decode("utf-8", errors="ignore")

    return {
        "message": "Serato file received",
        "size_bytes": len(file_content),
        "preview": decoded_preview,
        "tracks": [],
    }