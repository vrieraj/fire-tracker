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
