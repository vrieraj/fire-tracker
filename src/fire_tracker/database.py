"""
SQLite persistence layer for tracked fires.

Main table 'fires' with upsert by (source, external_id).
History table 'fire_history' for status changes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class FireDatabase:
    def __init__(self, db_path: str | Path = 'data/fires.db'):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS fires (
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    source_url TEXT,
                    latitude REAL,
                    longitude REAL,
                    municipality TEXT,
                    province TEXT,
                    region TEXT,
                    country TEXT DEFAULT 'ES',
                    status TEXT DEFAULT 'unknown',
                    fire_type TEXT,
                    detection_date TEXT,
                    extinction_date TEXT,
                    area_ha REAL,
                    resources TEXT,
                    raw_data TEXT,
                    last_updated TEXT NOT NULL,
                    PRIMARY KEY (source, external_id)
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS fire_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    changed_at TEXT NOT NULL,
                    FOREIGN KEY (source, external_id) REFERENCES fires(source, external_id)
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_fires_status ON fires(status)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_fires_country ON fires(country)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_fires_coords ON fires(latitude, longitude)')

            # FRP satellite detections table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS frp_detections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    longitude REAL NOT NULL,
                    latitude REAL NOT NULL,
                    frp_mw REAL NOT NULL,
                    confidence REAL,
                    frp_uncertainty REAL,
                    pixel_size_km2 REAL,
                    acquisition_time TEXT NOT NULL,
                    bt_mir REAL,
                    bt_tir REAL,
                    inserted_at TEXT NOT NULL,
                    UNIQUE(longitude, latitude, acquisition_time)
                )
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_frp_acq ON frp_detections(acquisition_time)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_frp_coords ON frp_detections(latitude, longitude)')
            conn.commit()

    def upsert(self, fire: dict) -> bool:
        with self._connect() as conn:
            existing = conn.execute(
                'SELECT status FROM fires WHERE source = ? AND external_id = ?',
                (fire['source'], fire['external_id']),
            ).fetchone()

            if existing:
                old_status = existing['status']
                if old_status != fire.get('status', 'unknown'):
                    conn.execute(
                        'INSERT INTO fire_history (source, external_id, status, changed_at) '
                        'VALUES (?, ?, ?, ?)',
                        (fire['source'], fire['external_id'],
                         fire.get('status', 'unknown'),
                         datetime.now(timezone.utc).isoformat()),
                    )

                columns = [k for k in fire.keys() if k not in ('source', 'external_id')]
                set_clause = ', '.join(f'{c} = ?' for c in columns)
                values = [self._serialize(fire, c) for c in columns] + [fire['source'], fire['external_id']]
                conn.execute(
                    f'UPDATE fires SET {set_clause} WHERE source = ? AND external_id = ?',
                    values,
                )
            else:
                columns = list(fire.keys())
                placeholders = ', '.join('?' for _ in columns)
                values = [self._serialize(fire, c) for c in columns]
                conn.execute(
                    f'INSERT INTO fires ({", ".join(columns)}) VALUES ({placeholders})',
                    values,
                )
                conn.execute(
                    'INSERT INTO fire_history (source, external_id, status, changed_at) '
                    'VALUES (?, ?, ?, ?)',
                    (fire['source'], fire['external_id'],
                     fire.get('status', 'unknown'),
                     datetime.now(timezone.utc).isoformat()),
                )
            conn.commit()
        return True

    def get_active_fires(self, country: str | None = None) -> list[dict]:
        active_statuses = ('active', 'controlled', 'stabilized', 'declarado')
        placeholders = ', '.join(f"'{s}'" for s in active_statuses)
        query = f'SELECT * FROM fires WHERE status IN ({placeholders})'
        params = []
        if country:
            query += ' AND country = ?'
            params.append(country)
        query += ' ORDER BY last_updated DESC'

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_all_fires(self, limit: int = 500, offset: int = 0) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM fires ORDER BY last_updated DESC LIMIT ? OFFSET ?',
                (limit, offset),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_fire(self, source: str, external_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                'SELECT * FROM fires WHERE source = ? AND external_id = ?',
                (source, external_id),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_history(self, source: str, external_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM fire_history WHERE source = ? AND external_id = ? '
                'ORDER BY changed_at DESC',
                (source, external_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def purge_extinguished(self, older_than_days: int = 7):
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM fires WHERE status = 'extinguished' "
                "AND last_updated < datetime('now', ?)",
                (f'-{older_than_days} days',),
            )
            conn.commit()

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute('SELECT COUNT(*) FROM fires').fetchone()[0]

    @staticmethod
    def _serialize(fire: dict, key: str) -> Any:
        val = fire.get(key)
        if isinstance(val, (dict, list)):
            return json.dumps(val, ensure_ascii=False)
        return val

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        for key in ('resources', 'raw_data'):
            if d.get(key):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    # ── FRP methods ──────────────────────────────────────────────

    def insert_frp_detections(self, detections: list[dict]) -> int:
        """Insert FRP detections, skip duplicates. Returns count inserted."""
        now = datetime.now(timezone.utc).isoformat()
        inserted = 0
        with self._connect() as conn:
            for d in detections:
                try:
                    cur = conn.execute(
                        'INSERT OR IGNORE INTO frp_detections '
                        '(longitude, latitude, frp_mw, confidence, frp_uncertainty, '
                        'pixel_size_km2, acquisition_time, bt_mir, bt_tir, inserted_at) '
                        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        (
                            d['longitude'], d['latitude'], d['frp_mw'],
                            d.get('confidence'), d.get('frp_uncertainty'),
                            d.get('pixel_size_km2'), d['acquisition_time'],
                            d.get('bt_mir'), d.get('bt_tir'), now,
                        ),
                    )
                    if cur.rowcount > 0:
                        inserted += 1
                except Exception as e:
                    logger.debug('FRP insert error: %s', e)
            conn.commit()
        return inserted

    def get_frp_detections(self, hours: int = 24) -> list[dict]:
        """Get FRP detections from the last N hours."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM frp_detections "
                "WHERE acquisition_time >= datetime('now', ?) "
                "ORDER BY acquisition_time ASC",
                (f'-{hours} hours',),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_frp_detections(self, hours: int = 24) -> int:
        with self._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM frp_detections "
                "WHERE acquisition_time >= datetime('now', ?)",
                (f'-{hours} hours',),
            ).fetchone()[0]

    def get_frp_by_bbox(
        self,
        lat_min: float,
        lat_max: float,
        lon_min: float,
        lon_max: float,
        hours: int = 24,
        min_confidence: float = 0.3,
    ) -> list[dict]:
        """Get FRP detections within a bounding box."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM frp_detections "
                "WHERE acquisition_time >= datetime('now', ?) "
                "AND latitude BETWEEN ? AND ? "
                "AND longitude BETWEEN ? AND ? "
                "AND confidence >= ? "
                "ORDER BY acquisition_time ASC",
                (f'-{hours} hours', lat_min, lat_max, lon_min, lon_max, min_confidence),
            ).fetchall()
        return [dict(r) for r in rows]
