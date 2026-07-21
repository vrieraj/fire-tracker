"""Tests for FireDatabase dual persistence (SQLite / PostgreSQL)."""

import os
import tempfile
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

import pytest

from fire_tracker.database import FireDatabase


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "test_fires.db")


@pytest.fixture
def db(db_path):
    import fire_tracker.database as mod
    original = mod.DATABASE_URL
    mod.DATABASE_URL = None
    d = FireDatabase(db_path=db_path)
    yield d
    mod.DATABASE_URL = original


def test_database_sqlite_local(db):
    """Verificar que sin DATABASE_URL usa SQLite."""
    assert not db._is_pg
    assert db.count() == 0
    fires = db.get_active_fires()
    assert isinstance(fires, list)
    assert len(fires) == 0


def test_database_postgresql():
    """Verificar que con DATABASE_URL usa PostgreSQL."""
    import fire_tracker.database as mod
    fake_url = "postgresql://user:pass@localhost:5432/testdb"
    original = mod.DATABASE_URL
    mod.DATABASE_URL = fake_url
    try:
        mock_conn = MagicMock()
        with patch("psycopg2.connect", return_value=mock_conn):
            test_db = FireDatabase(db_path="/tmp/test_pg.db")
            assert test_db._is_pg
    finally:
        mod.DATABASE_URL = original


def test_upsert_fire_sqlite(db):
    """Verificar upsert funciona en SQLite."""
    fire = {
        "source": "test_source",
        "external_id": "fire_001",
        "latitude": 40.0,
        "longitude": -3.5,
        "municipality": "Madrid",
        "status": "active",
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    result = db.upsert(fire)
    assert result is True
    assert db.count() == 1

    retrieved = db.get_fire("test_source", "fire_001")
    assert retrieved is not None
    assert retrieved["latitude"] == 40.0
    assert retrieved["status"] == "active"


def test_upsert_fire_postgresql():
    """Verificar upsert funciona en PostgreSQL (mock)."""
    import fire_tracker.database as mod
    fake_url = "postgresql://user:pass@localhost:5432/testdb"
    original = mod.DATABASE_URL
    mod.DATABASE_URL = fake_url
    try:
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_conn.cursor.return_value = mock_cursor

        with patch("psycopg2.connect", return_value=mock_conn):
            test_db = FireDatabase(db_path="/tmp/test_pg.db")
            fire = {
                "source": "test_source",
                "external_id": "fire_001",
                "latitude": 40.0,
                "longitude": -3.5,
                "status": "active",
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            result = test_db.upsert(fire)
            assert result is True
            mock_cursor.execute.assert_called()
    finally:
        mod.DATABASE_URL = original


def test_state_history(db):
    """Verificar que fire_history se crea correctamente."""
    fire1 = {
        "source": "test_source",
        "external_id": "fire_001",
        "latitude": 40.0,
        "longitude": -3.5,
        "status": "active",
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    db.upsert(fire1)

    fire2 = {
        "source": "test_source",
        "external_id": "fire_001",
        "latitude": 40.0,
        "longitude": -3.5,
        "status": "controlled",
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    db.upsert(fire2)

    history = db.get_history("test_source", "fire_001")
    assert len(history) == 2
    statuses = [h["status"] for h in history]
    assert "controlled" in statuses
    assert "active" in statuses


def test_upsert_update_existing(db):
    """Verificar que upsert actualiza registros existentes."""
    fire = {
        "source": "src",
        "external_id": "ext_1",
        "latitude": 38.0,
        "longitude": -4.0,
        "status": "active",
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    db.upsert(fire)
    assert db.count() == 1

    fire["status"] = "extinguished"
    db.upsert(fire)
    assert db.count() == 1

    retrieved = db.get_fire("src", "ext_1")
    assert retrieved["status"] == "extinguished"


def test_frp_insert_sqlite(db):
    """Verificar inserción de FRP en SQLite."""
    detections = [
        {
            "longitude": -3.5,
            "latitude": 40.0,
            "frp_mw": 15.0,
            "confidence": 0.8,
            "acquisition_time": "20260101120000",
        }
    ]
    inserted = db.insert_frp_detections(detections)
    assert inserted == 1
    assert db.count_frp_detections() == 1
