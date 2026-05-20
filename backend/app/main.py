from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware

from app.parsers.rekordbox_parser import parse_rekordbox_xml
from app.parsers.serato_parser import parse_serato_metadata

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/upload/rekordbox")
async def upload_rekordbox(file: UploadFile = File(...)):
    contents = await file.read()

    parsed_tracks = parse_rekordbox_xml(contents)

    return {
        "filename": file.filename,
        "tracksFound": len(parsed_tracks),
        "tracks": parsed_tracks[:100],
    }

@app.post("/upload/serato")
async def upload_serato(file: UploadFile = File(...)):
    contents = await file.read()

    parsed_data = parse_serato_metadata(contents)

    return parsed_data