"""Tests for robustness fixes (2026-08-05)."""

from unittest.mock import Mock, patch

from fire_tracker.scrapers.base import FireScraper
from fire_tracker.weather import Location, _province, _region, _short_name
from fire_tracker.frp_locator import _MIN_CONFIDENCE, locate_fire


def test_status_normalize_non_string():
    """_status_normalize no lanza AttributeError con valores no-string."""
    assert FireScraper._status_normalize(None, 't') == 'unknown'
    assert FireScraper._status_normalize(3, 't') == 'unknown'
    assert FireScraper._status_normalize(True, 't') == 'unknown'
    assert FireScraper._status_normalize('Activo', 't') == 'active'


def test_short_name_and_province():
    """Helpers de address extraen nombre corto y provincia."""
    addr = {'municipality': 'Vinuesa', 'province': 'Soria', 'state': 'Castilla y León', 'country_code': 'es'}
    assert _short_name(addr) == 'Vinuesa'
    assert _province(addr) == 'Soria'
    assert _region(addr) == 'Castilla y León'
    addr_pt = {'city': 'Lisboa', 'county': 'Lisboa', 'country_code': 'pt'}
    assert _short_name(addr_pt) == 'Lisboa'
    assert _province(addr_pt) == 'Lisboa'


def test_geocode_short_name():
    """geocode devuelve nombre corto y país ISO (no display_name completo)."""
    payload = [{
        'lat': '41.9117774', 'lon': '-2.7631085',
        'display_name': 'Vinuesa, Soria, Castilla y León, 42150, España',
        'address': {
            'municipality': 'Vinuesa', 'province': 'Soria',
            'state': 'Castilla y León', 'country_code': 'es',
        },
    }]
    mock_resp = Mock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = payload
    with patch('fire_tracker.weather.get_elevation', return_value=1107.0), \
         patch('fire_tracker.weather.requests.get', return_value=mock_resp):
        from fire_tracker.weather import geocode
        loc = geocode('Vinuesa')
    assert loc is not None
    assert loc.name == 'Vinuesa'
    assert loc.municipality == 'Vinuesa'
    assert loc.province == 'Soria'
    assert loc.region == 'Castilla y León'
    assert loc.country == 'ES'


def test_api_geocode_contract():
    """/api/geocode devuelve display_name/admin1/municipality/province."""
    from fire_tracker.api.app import app
    loc = Location(
        name='Vinuesa', latitude=41.9, longitude=-2.76, elevation=1107.0,
        municipality='Vinuesa', province='Soria', region='Castilla y León', country='ES',
    )
    with patch('fire_tracker.api.app.geocode', return_value=loc):
        app.config['TESTING'] = True
        with app.test_client() as c:
            resp = c.get('/api/geocode?q=Vinuesa')
    assert resp.status_code == 200
    r = resp.get_json()['results'][0]
    assert r['municipality'] == 'Vinuesa'
    assert r['admin1'] == 'Soria'
    assert r['province'] == 'Soria'
    assert r['country'] == 'ES'
    assert r['display_name']


def test_frp_locator_uses_short_fields():
    """frp_locator usa municipality/province de Location, no display_name."""
    loc = Location(
        name='Vinuesa', latitude=41.9, longitude=-2.76,
        municipality='Vinuesa', province='Soria', region='Castilla y León', country='ES',
    )
    with patch('fire_tracker.frp_locator.geocode', return_value=loc), \
         patch('fire_tracker.frp_locator.reverse_geocode', return_value=None), \
         patch('fire_tracker.frp_locator.FireDatabase'):
        fl = locate_fire(municipality='Vinuesa', province='Soria', db_path=':memory:')
    assert fl is not None
    assert fl.municipality == 'Vinuesa'
    assert fl.province == 'Soria'
    assert fl.region == 'Castilla y León'
    assert fl.country == 'ES'
    assert fl.source == 'geocode'


def test_frp_confidence_threshold_unified():
    """Los umbrales de confianza están unificados a 0.5."""
    import inspect
    from fire_tracker.database import FireDatabase
    from fire_tracker.frp import _MIN_CONFIDENCE as frp_min
    db_default = inspect.signature(FireDatabase.get_frp_by_bbox).parameters['min_confidence'].default
    assert _MIN_CONFIDENCE == 0.5
    assert frp_min == 0.5
    assert db_default == 0.5
