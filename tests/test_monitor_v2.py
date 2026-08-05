"""Tests for monitor.py v2 — X.com fire monitor pipeline.

Covers: mandatory #IF hashtag, regional filter, balanced verification
(FRP / official / corroborated / perimeter), 10 km merge, unverified
upgrade, cross-source discard, auto-expiry and perimeter linking.
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from fire_tracker.database import FireDatabase
from fire_tracker.frp_locator import FireLocation
from fire_tracker.xmonitor import XTweet

from fire_tracker import monitor as mon


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, 'test_fires.db')


@pytest.fixture
def db(db_path):
    import fire_tracker.database as mod
    original = mod.DATABASE_URL
    mod.DATABASE_URL = None
    d = FireDatabase(db_path=db_path)
    yield d
    mod.DATABASE_URL = original


def _tweet(tweet_id, text, author='ciudadano_1', minutes_ago=0):
    return XTweet(
        tweet_id=str(tweet_id),
        text=text,
        author_handle=author,
        author_name=author,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


def _loc(region='Aragón', municipality='Jaca', province='Huesca',
         lat=42.57, lon=-0.55, source='geocode', frp_count=0, frp_max_mw=0.0):
    return FireLocation(
        latitude=lat, longitude=lon, municipality=municipality,
        province=province, region=region, country='ES',
        source=source, frp_count=frp_count, frp_max_mw=frp_max_mw,
    )


def _stats():
    return {
        'new_fires': 0,
        'duplicates': 0,
        'geocode_failures': 0,
        'no_hashtag': 0,
    }


def _process(db, tweets, loc_factory):
    with patch('fire_tracker.monitor.locate_fire',
               side_effect=lambda **kw: loc_factory(kw.get('municipality', ''))):
        stats = _stats()
        mon._process_tweets(db, tweets, stats)
    return stats


def _insert_fire(db, **overrides):
    fire = {
        'source': 'xmonitor',
        'external_id': 'x_fire_1',
        'latitude': 42.57,
        'longitude': -0.55,
        'municipality': 'Jaca',
        'province': 'Huesca',
        'region': 'Aragón',
        'country': 'ES',
        'status': 'active',
        'fire_type': 'wildfire',
        'detection_date': datetime.now(timezone.utc).isoformat(),
        'last_updated': datetime.now(timezone.utc).isoformat(),
        'verified': 'corroborated',
        'raw_data': {'tweets': [{
            'id': '1',
            'created_at': (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
        }]},
    }
    fire.update(overrides)
    db.upsert(fire)


def test_extract_if_hashtag():
    assert mon._extract_if_hashtag('Incendio #IFJaca') == '#IFJaca'
    assert mon._extract_if_hashtag('Incendio #IFVillaDeMaso') == '#IFVillaDeMaso'
    assert mon._extract_if_hashtag('Sin hashtag') is None


def test_requires_if_hashtag(db):
    """Un tuit sin #IF{Municipio} no genera candidato ni incendio."""
    tweets = [_tweet('1', 'Gran incendio en Jaca, bomberos en camino')]
    stats = _process(db, tweets, lambda m: _loc())
    assert stats['no_hashtag'] == 1
    assert db.count() == 0


def test_unmonitored_region_filtered(db):
    """Región con scraper oficial (Andalucía) no se monitoriza."""
    tweets = [_tweet('2', '#IFAlhaurínElGrande incendio forestal')]
    stats = _process(db, tweets, lambda m: _loc(region='Andalucía', municipality=m))
    assert stats.get('unmonitored_region') == 1
    assert db.count() == 0


def test_outside_iberia_filtered(db):
    """Candidato fuera de la península se descarta."""
    tweets = [_tweet('3', '#IFParís incendio')]
    stats = _process(db, tweets, lambda m: _loc(region='Île-de-France', lat=48.8, lon=2.3))
    assert stats.get('outside_region') == 1
    assert db.count() == 0


def test_frp_verified_active(db):
    """Evidencia FRP satelital → fire verified='frp', status='active'."""
    tweets = [_tweet('4', '#IFJaca incendio activo')]
    stats = _process(db, tweets, lambda m: _loc(source='frp', frp_count=3, frp_max_mw=25.0))
    assert stats['new_fires'] == 1
    assert db.count() == 1
    fire = db.get_fire('xmonitor', 'x_4')
    assert fire['verified'] == 'frp'
    assert fire['status'] == 'active'


def test_official_account_verified(db):
    """Cuenta oficial Tier-1 → verified='official' sin necesidad de FRP."""
    tweets = [_tweet('5', '#IFJaca incendio forestal', author='112Arago')]
    stats = _process(db, tweets, lambda m: _loc())
    assert stats['new_fires'] == 1
    fire = db.get_fire('xmonitor', 'x_5')
    assert fire['verified'] == 'official'
    assert fire['status'] == 'active'


def test_corroborated_two_tweets_one_entity(db):
    """Dos tuits independientes de la misma zona → corroborated, 1 entidad."""
    tweets = [
        _tweet('6', '#IFJaca humo visible', author='ciudadano_a', minutes_ago=60),
        _tweet('7', '#IFJaca llama en monte', author='ciudadano_b', minutes_ago=30),
    ]
    stats = _process(db, tweets, lambda m: _loc())
    assert stats['new_fires'] == 1
    assert stats['duplicates'] == 1
    assert db.count() == 1
    fire = db.get_fire('xmonitor', 'x_6')
    assert fire['verified'] == 'corroborated'
    assert len(fire['raw_data']['tweets']) == 2


def test_single_citizen_tweet_unverified_hidden(db):
    """Un único tuit sin FRP ni oficial ni corroboración → oculto unverified."""
    tweets = [_tweet('8', '#IFJaca posible incendio')]
    stats = _process(db, tweets, lambda m: _loc())
    assert db.count() == 1
    fire = db.get_fire('xmonitor', 'x_8')
    assert fire['status'] == 'unverified'
    assert fire['verified'] is None
    assert stats.get('unverified') == 1


def test_merge_unverified_same_fire(db):
    """Dos tuits del mismo incendio (>6h, no corroboración) → 1 entidad."""
    tweets = [
        _tweet('9', '#IFJaca incendio', minutes_ago=500),
        _tweet('10', '#IFJaca sigue activo', minutes_ago=400),
    ]
    stats = _process(db, tweets, lambda m: _loc())
    assert db.count() == 1
    fire = db.get_fire('xmonitor', 'x_9')
    assert fire is not None
    assert fire['external_id'] == 'x_9'
    assert len(fire['raw_data']['tweets']) == 2
    assert stats['duplicates'] == 1


def test_upgrade_unverified_when_frp_appears(db):
    """Unverified asciende a active cuando llega evidencia FRP (misma entidad)."""
    _insert_fire(db, status='unverified', verified=None)
    tweets = [_tweet('11', '#IFJaca incendio confirmado')]
    stats = _process(db, tweets, lambda m: _loc(source='frp', frp_count=2))
    assert stats.get('upgraded') == 1
    assert db.count() == 1
    fire = db.get_fire('xmonitor', 'x_fire_1')
    assert fire is not None
    assert fire['status'] == 'active'
    assert fire['verified'] == 'frp'


def test_cross_source_discarded(db):
    """Candidato cerca de incendio ya reportado por otra fuente → descartado."""
    _insert_fire(db, source='infoca', external_id='infoca_1')
    tweets = [_tweet('12', '#IFJaca incendio')]
    stats = _process(db, tweets, lambda m: _loc())
    assert stats.get('cross_source') == 1
    assert db.count() == 1


def test_expire_stale(db):
    """Sin FRP ni tuits recientes → extinguished. Con tuit reciente → activo."""
    _insert_fire(db, external_id='x_stale', raw_data={'tweets': [{
        'id': 's1',
        'created_at': (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
    }]})
    _insert_fire(db, external_id='x_fresh', raw_data={'tweets': [{
        'id': 'f1',
        'created_at': datetime.now(timezone.utc).isoformat(),
    }]})
    stats = _stats()
    with patch('fire_tracker.monitor.FireDatabase.get_frp_near', return_value=[]):
        mon._expire_stale(db, stats)
    assert db.get_fire('xmonitor', 'x_stale')['status'] == 'extinguished'
    assert db.get_fire('xmonitor', 'x_fresh')['status'] == 'active'
    assert stats.get('expired') == 1


def test_perimeter_verifies_and_links(db):
    """Dentro de un perímetro EFFIS reciente → corroborated y vinculado."""
    db.upsert_perimeters([{
        'id': 'perm_1',
        'fire_date': datetime.now(timezone.utc).isoformat(),
        'country': 'ES',
        'province': 'Huesca',
        'commune': 'Jaca',
        'area_ha': 150.0,
        'geometry': {
            'type': 'Polygon',
            'coordinates': [[[-0.58, 42.55], [-0.52, 42.55], [-0.52, 42.60],
                             [-0.58, 42.60], [-0.58, 42.55]]],
        },
    }])
    tweets = [_tweet('13', '#IFJaca incendio')]
    stats = _process(db, tweets, lambda m: _loc())
    assert db.count() == 1
    fire = db.get_fire('xmonitor', 'x_13')
    assert fire['verified'] == 'corroborated'
    assert fire['raw_data']['perimeter_id'] == 'perm_1'
    assert fire['raw_data']['perimeter_area_ha'] == 150.0
    assert stats.get('perimeters_linked') == 1


def test_chronology_url_generated(db):
    """El fire lleva cronología X por hashtag #IF."""
    tweets = [_tweet('14', '#IFJaca incendio', author='112Arago')]
    _process(db, tweets, lambda m: _loc())
    fire = db.get_fire('xmonitor', 'x_14')
    url = fire['raw_data'].get('chronology_url', '')
    assert 'x.com/search' in url
    assert '%23IFJaca' in url or 'IFJaca' in url
