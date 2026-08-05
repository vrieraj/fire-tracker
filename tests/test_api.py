"""Tests for cron endpoints (Hito 4)."""

from unittest.mock import patch

import pytest

from fire_tracker.api.app import app


@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def test_ping_endpoint(client):
    """GET /ping retorna 200 con {"status": "ok"}."""
    resp = client.get('/ping')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data == {"status": "ok"}


def test_cron_run_post(client):
    """POST /api/cron/run retorna 200 con stats."""
    mock_stats = {"total_raw": 0, "total_after_dedup": 0, "sources": {}}
    with patch("fire_tracker.api.app.FireOrchestrator") as MockOrch:
        MockOrch.return_value.run.return_value = mock_stats
        with patch("fire_tracker.monitor.run_monitor", return_value={"new": 0}):
            resp = client.post('/api/cron/run')
    assert resp.status_code == 200
    data = resp.get_json()
    assert "scrapers" in data
    assert "monitor" in data


def test_cron_scrapers_post(client):
    """POST /api/cron/scrapers retorna 200 con stats."""
    mock_stats = {"total_raw": 0, "total_after_dedup": 0, "sources": {}}
    with patch("fire_tracker.api.app.FireOrchestrator") as MockOrch:
        MockOrch.return_value.run.return_value = mock_stats
        resp = client.post('/api/cron/scrapers')
    assert resp.status_code == 200
    data = resp.get_json()
    assert "total_raw" in data


def test_cron_monitor_post(client):
    """POST /api/cron/monitor retorna 200 con stats."""
    with patch("fire_tracker.monitor.run_monitor", return_value={"new": 0}):
        resp = client.post('/api/cron/monitor')
    assert resp.status_code == 200
    data = resp.get_json()
    assert "new" in data


def test_cron_stations_post(client):
    """POST /api/cron/stations retorna 200."""
    resp = client.post('/api/cron/stations')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"


def test_cron_run_get_rejected(client):
    """GET /api/cron/run retorna 405."""
    resp = client.get('/api/cron/run')
    assert resp.status_code == 405


def test_tracked_exposes_verified_and_perimeter(client):
    """/api/fires/tracked expone verified y perímetro EFFIS."""
    fake_fire = {
        "source": "xmonitor",
        "external_id": "x_1",
        "latitude": 42.57,
        "longitude": -0.55,
        "municipality": "Jaca",
        "province": "Huesca",
        "region": "Aragón",
        "country": "ES",
        "status": "active",
        "verified": "corroborated",
        "raw_data": {
            "perimeter_id": "perm_1",
            "perimeter_area_ha": 150.0,
            "perimeter_commune": "Jaca",
            "chronology_url": "https://x.com/search?q=%23IFJaca",
        },
        "last_updated": "2026-08-05T22:00:00Z",
    }
    with patch("fire_tracker.api.app._db.get_active_fires", return_value=[fake_fire]):
        resp = client.get("/api/fires/tracked")
    assert resp.status_code == 200
    props = resp.get_json()["features"][0]["properties"]
    assert props["verified"] == "corroborated"
    assert props["perimeter_id"] == "perm_1"
    assert props["perimeter_area_ha"] == 150.0
    assert props["perimeter_commune"] == "Jaca"
