from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from app.models import RekordboxUploadResponse
from app.parsers.serato_parser import parse_serato_metadata
from app.services import rekordbox_service
from app.services.rekordbox_service import FileTooLargeError, LibraryError

MAX_TRACKS_RETURNED = 100

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def to_http_error(error: LibraryError) -> HTTPException:
    status_code = 413 if isinstance(error, FileTooLargeError) else 400
    return HTTPException(status_code=status_code, detail=str(error))


@app.post("/upload/rekordbox", response_model=RekordboxUploadResponse)
async def upload_rekordbox(file: UploadFile = File(...)):
    try:
        # Reject oversized uploads before reading them into memory.
        rekordbox_service.check_size(file.size)

        contents = await file.read()
        tracks = rekordbox_service.parse_rekordbox_bytes(contents, file.filename)
    except LibraryError as error:
        raise to_http_error(error) from None

    return {
        "filename": file.filename,
        "tracksFound": len(tracks),
        "tracks": tracks[:MAX_TRACKS_RETURNED],
    }


@app.post("/upload/serato")
async def upload_serato(file: UploadFile = File(...)):
    contents = await file.read()

    parsed_data = parse_serato_metadata(contents)

    return parsed_data
