import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import app
from app.services import rekordbox_service

client = TestClient(app)

VALID_XML = b"""<DJ_PLAYLISTS><COLLECTION>
<TRACK TrackID="1" Name="Song" Artist="Artist" Location="file://localhost/a.mp3">
<POSITION_MARK Name="Drop" Type="0" Start="32.5" Num="0" Red="40" Green="226" Blue="20"/>
</TRACK></COLLECTION></DJ_PLAYLISTS>"""


def upload(contents: bytes, filename: str = "rekordbox.xml"):
    return client.post("/upload/rekordbox", files={"file": (filename, contents, "text/xml")})


class TestRekordboxUpload:
    def test_valid_file(self):
        response = upload(VALID_XML)

        assert response.status_code == 200

        body = response.json()

        assert body["filename"] == "rekordbox.xml"
        assert body["tracksFound"] == 1
        assert body["tracks"][0]["trackTitle"] == "Song"
        assert body["tracks"][0]["cues"][0]["color"] == "#28e214"
        assert body["tracks"][0]["cues"][0]["cueType"] == "hotcue"

    def test_empty_collection_is_valid(self):
        response = upload(b"<DJ_PLAYLISTS><COLLECTION/></DJ_PLAYLISTS>")

        assert response.status_code == 200
        assert response.json()["tracksFound"] == 0

    def test_uppercase_extension_accepted(self):
        assert upload(VALID_XML, "EXPORT.XML").status_code == 200

    @pytest.mark.parametrize("filename", ["library.txt", "library", "library.xml.zip"])
    def test_wrong_extension_rejected(self, filename):
        response = upload(VALID_XML, filename)

        assert response.status_code == 400
        assert ".xml" in response.json()["detail"]

    def test_empty_file_rejected(self):
        response = upload(b"   \n")

        assert response.status_code == 400
        assert "empty" in response.json()["detail"].lower()

    def test_malformed_xml_rejected(self):
        response = upload(b"<DJ_PLAYLISTS><COLLECTION>")

        assert response.status_code == 400
        assert "not valid XML" in response.json()["detail"]

    def test_xml_without_collection_rejected(self):
        response = upload(b"<html><body/></html>")

        assert response.status_code == 400
        assert "Rekordbox" in response.json()["detail"]

    def test_oversized_file_rejected(self, monkeypatch):
        monkeypatch.setattr(rekordbox_service, "MAX_FILE_BYTES", 10)

        response = upload(VALID_XML)

        assert response.status_code == 413

    def test_missing_file_rejected(self):
        assert client.post("/upload/rekordbox").status_code == 422

    def test_track_list_is_capped(self, monkeypatch):
        monkeypatch.setattr(main, "MAX_TRACKS_RETURNED", 2)
        tracks = "".join(f'<TRACK TrackID="{i}" Name="T{i}"/>' for i in range(5))

        body = upload(f"<DJ_PLAYLISTS><COLLECTION>{tracks}</COLLECTION></DJ_PLAYLISTS>".encode()).json()

        assert body["tracksFound"] == 5
        assert len(body["tracks"]) == 2
