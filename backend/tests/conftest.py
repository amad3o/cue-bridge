from pathlib import Path

import pytest

from cuebridge.services import rekordbox_db


@pytest.fixture(autouse=True)
def never_the_real_rekordbox_database(monkeypatch, tmp_path_factory):
    """A test must never read (or write) the developer's own Rekordbox database.

    The database is found from Rekordbox's settings file, so point both places it looks at nothing.
    """
    monkeypatch.setattr(rekordbox_db, "SETTINGS_FILE", Path(tmp_path_factory.getbasetemp()) / "no-rekordbox-settings")
    monkeypatch.setattr(rekordbox_db, "HOME_DATABASE", Path(tmp_path_factory.getbasetemp()) / "no-master.db")
