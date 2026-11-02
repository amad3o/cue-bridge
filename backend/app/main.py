import xml.etree.ElementTree as ET

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from app.models import RekordboxUploadResponse
from app.parsers.rekordbox_parser import RekordboxParseError, parse_rekordbox_xml
from app.parsers.serato_parser import parse_serato_metadata

MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB
MAX_TRACKS_RETURNED = 100

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/upload/rekordbox", response_model=RekordboxUploadResponse)
async def upload_rekordbox(file: UploadFile = File(...)):
    filename = file.filename or ""

    if not filename.lower().endswith(".xml"):
        raise HTTPException(status_code=400, detail="Please upload a Rekordbox .xml file.")

    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File is too large (max 100 MB).")

    contents = await file.read()

    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File is too large (max 100 MB).")

    if not contents.strip():
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")

    try:
        parsed_tracks = parse_rekordbox_xml(contents)
    except ET.ParseError:
        raise HTTPException(status_code=400, detail="The file is not valid XML.")
    except RekordboxParseError:
        raise HTTPException(
            status_code=400,
            detail="This doesn't look like a Rekordbox library export.",
        )

    return {
        "filename": filename,
        "tracksFound": len(parsed_tracks),
        "tracks": parsed_tracks[:MAX_TRACKS_RETURNED],
    }


@app.post("/upload/serato")
async def upload_serato(file: UploadFile = File(...)):
    contents = await file.read()

    parsed_data = parse_serato_metadata(contents)

    return parsed_data
