"""
Dual persistence layer: SQLite (local dev) / PostgreSQL (Supabase).

Main table 'fires' with upsert by (source, external_id).
History table 'fire_history' for status changes.
FRP satellite detections table.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")


class FireDatabase:
    def __init__(self, db_path: str | Path = 'data/fires.db'):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._is_pg = bool(DATABASE_URL)
        self._init_db()

    def _connect(self):
        if self._is_pg:
            import psycopg2
            conn = psycopg2.connect(DATABASE_URL)
            conn.autocommit = False
            return conn
        else:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA foreign_keys=ON')
            return conn

    def _q(self, param_char: str = '?') -> str:
        return param_char

    def _init_pg(self):
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute('''
                CREATE TABLE IF NOT EXISTS fires (
                    source VARCHAR(50) NOT NULL,
                    external_id VARCHAR(100) NOT NULL,
                    source_url TEXT,
                    latitude DOUBLE PRECISION,
                    longitude DOUBLE PRECISION,
                    municipality TEXT,
                    province TEXT,
                    region TEXT,
                    country VARCHAR(10) DEFAULT 'ES',
                    status VARCHAR(50) DEFAULT 'unknown',
                    fire_type VARCHAR(50),
                    detection_date TEXT,
                    extinction_date TEXT,
                    area_ha DOUBLE PRECISION,
                    resources TEXT,
                    raw_data TEXT,
                    last_updated TEXT NOT NULL,
                    PRIMARY KEY (source, external_id)
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS fire_history (
                    id SERIAL PRIMARY KEY,
                    source VARCHAR(50) NOT NULL,
                    external_id VARCHAR(100) NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    changed_at TEXT NOT NULL,
                    FOREIGN KEY (source, external_id) REFERENCES fires(source, external_id)
                )
            ''')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_fires_status ON fires(status)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_fires_country ON fires(country)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_fires_coords ON fires(latitude, longitude)')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS frp_detections (
                    id SERIAL PRIMARY KEY,
                    longitude DOUBLE PRECISION NOT NULL,
                    latitude DOUBLE PRECISION NOT NULL,
                    frp_mw DOUBLE PRECISION NOT NULL,
                    confidence DOUBLE PRECISION,
                    frp_uncertainty DOUBLE PRECISION,
                    pixel_size_km2 DOUBLE PRECISION,
                    acquisition_time TEXT NOT NULL,
                    bt_mir DOUBLE PRECISION,
                    bt_tir DOUBLE PRECISION,
                    inserted_at TEXT NOT NULL,
                    UNIQUE(longitude, latitude, acquisition_time)
                )
            ''')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_frp_acq ON frp_detections(acquisition_time)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_frp_coords ON frp_detections(latitude, longitude)')
            conn.commit()
        finally:
            conn.close()

    def _init_sqlite(self):
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

    def _init_db(self):
        if self._is_pg:
            self._init_pg()
        else:
            self._init_sqlite()

    def upsert(self, fire: dict) -> bool:
        if self._is_pg:
            return self._upsert_pg(fire)
        return self._upsert_sqlite(fire)

    def _upsert_sqlite(self, fire: dict) -> bool:
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

    def _upsert_pg(self, fire: dict) -> bool:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                'SELECT status FROM fires WHERE source = %s AND external_id = %s',
                (fire['source'], fire['external_id']),
            )
            existing = cur.fetchone()

            if existing:
                old_status = existing[0]
                if old_status != fire.get('status', 'unknown'):
                    cur.execute(
                        'INSERT INTO fire_history (source, external_id, status, changed_at) '
                        'VALUES (%s, %s, %s, %s)',
                        (fire['source'], fire['external_id'],
                         fire.get('status', 'unknown'),
                         datetime.now(timezone.utc).isoformat()),
                    )

                columns = [k for k in fire.keys() if k not in ('source', 'external_id')]
                set_clause = ', '.join(f'{c} = %s' for c in columns)
                values = [self._serialize(fire, c) for c in columns] + [fire['source'], fire['external_id']]
                cur.execute(
                    f'UPDATE fires SET {set_clause} WHERE source = %s AND external_id = %s',
                    values,
                )
            else:
                columns = list(fire.keys())
                placeholders = ', '.join('%s' for _ in columns)
                values = [self._serialize(fire, c) for c in columns]
                cur.execute(
                    f'INSERT INTO fires ({", ".join(columns)}) VALUES ({placeholders})',
                    values,
                )
                cur.execute(
                    'INSERT INTO fire_history (source, external_id, status, changed_at) '
                    'VALUES (%s, %s, %s, %s)',
                    (fire['source'], fire['external_id'],
                     fire.get('status', 'unknown'),
                     datetime.now(timezone.utc).isoformat()),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return True

    def get_active_fires(self, country: str | None = None) -> list[dict]:
        active_statuses = ('active', 'controlled', 'stabilized', 'declarado')
        if self._is_pg:
            placeholders = ', '.join('%s' for _ in active_statuses)
            query = f'SELECT * FROM fires WHERE status IN ({placeholders})'
            params = list(active_statuses)
        else:
            placeholders = ', '.join(f"'{s}'" for s in active_statuses)
            query = f'SELECT * FROM fires WHERE status IN ({placeholders})'
            params = []
        if country:
            ph = '%s' if self._is_pg else '?'
            query += f' AND country = {ph}'
            params.append(country)
        query += ' ORDER BY last_updated DESC'

        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(query, params)
            rows = cur.fetchall()
            if self._is_pg:
                cols = [d[0] for d in cur.description]
                return [self._pg_row_to_dict(dict(zip(cols, r)), r) for r in rows]
            return [self._row_to_dict(r) for r in rows]
        finally:
            if self._is_pg:
                conn.close()

    def get_all_fires(self, limit: int = 500, offset: int = 0) -> list[dict]:
        ph = '%s' if self._is_pg else '?'
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f'SELECT * FROM fires ORDER BY last_updated DESC {ph} OFFSET {ph}', (limit, offset))
            rows = cur.fetchall()
            if self._is_pg:
                cols = [d[0] for d in cur.description]
                return [self._pg_row_to_dict(dict(zip(cols, r)), r) for r in rows]
            return [self._row_to_dict(r) for r in rows]
        finally:
            if self._is_pg:
                conn.close()

    def get_fire(self, source: str, external_id: str) -> dict | None:
        ph = '%s' if self._is_pg else '?'
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f'SELECT * FROM fires WHERE source = {ph} AND external_id = {ph}', (source, external_id))
            row = cur.fetchone()
            if row is None:
                return None
            if self._is_pg:
                cols = [d[0] for d in cur.description]
                return self._pg_row_to_dict(dict(zip(cols, row)), row)
            return self._row_to_dict(row)
        finally:
            if self._is_pg:
                conn.close()

    def get_history(self, source: str, external_id: str) -> list[dict]:
        ph = '%s' if self._is_pg else '?'
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                f'SELECT * FROM fire_history WHERE source = {ph} AND external_id = {ph} '
                'ORDER BY changed_at DESC',
                (source, external_id),
            )
            rows = cur.fetchall()
            if self._is_pg:
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in rows]
            return [dict(r) for r in rows]
        finally:
            if self._is_pg:
                conn.close()

    def purge_extinguished(self, older_than_days: int = 7):
        if self._is_pg:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM fires WHERE status = 'extinguished' "
                    "AND last_updated::timestamp < NOW() - make_interval(days => %s)",
                    (older_than_days,),
                )
                conn.commit()
            finally:
                conn.close()
        else:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM fires WHERE status = 'extinguished' "
                    "AND last_updated < datetime('now', ?)",
                    (f'-{older_than_days} days',),
                )
                conn.commit()

    def count(self) -> int:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute('SELECT COUNT(*) FROM fires')
            return cur.fetchone()[0]
        finally:
            if self._is_pg:
                conn.close()

    @staticmethod
    def _serialize(fire: dict, key: str) -> Any:
        val = fire.get(key)
        if isinstance(val, (dict, list)):
            return json.dumps(val, ensure_ascii=False)
        return val

    @staticmethod
    def _row_to_dict(row) -> dict:
        d = dict(row)
        for key in ('resources', 'raw_data'):
            if d.get(key):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    @staticmethod
    def _pg_row_to_dict(row: dict, raw_tuple) -> dict:
        for key in ('resources', 'raw_data'):
            if row.get(key):
                try:
                    row[key] = json.loads(row[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return row

    # ── FRP methods ──────────────────────────────────────────────

    def insert_frp_detections(self, detections: list[dict]) -> int:
        """Insert FRP detections, skip duplicates. Returns count inserted."""
        now = datetime.now(timezone.utc).isoformat()
        if self._is_pg:
            return self._insert_frp_pg(detections, now)
        return self._insert_frp_sqlite(detections, now)

    def _insert_frp_sqlite(self, detections: list[dict], now: str) -> int:
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

    def _insert_frp_pg(self, detections: list[dict], now: str) -> int:
        conn = self._connect()
        inserted = 0
        try:
            cur = conn.cursor()
            for d in detections:
                try:
                    cur.execute(
                        'INSERT INTO frp_detections '
                        '(longitude, latitude, frp_mw, confidence, frp_uncertainty, '
                        'pixel_size_km2, acquisition_time, bt_mir, bt_tir, inserted_at) '
                        'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) '
                        'ON CONFLICT (longitude, latitude, acquisition_time) DO NOTHING',
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
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return inserted

    def get_frp_detections(self, hours: int = 24) -> list[dict]:
        """Get FRP detections from the last N hours."""
        if self._is_pg:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM frp_detections "
                    "WHERE CASE "
                    "  WHEN acquisition_time ~ '^[0-9]{14}$' "
                    "    THEN to_timestamp(acquisition_time, 'YYYYMMDDHH24MISS') "
                    "  ELSE acquisition_time::timestamp "
                    "END >= NOW() - make_interval(hours => %s) "
                    "ORDER BY acquisition_time ASC",
                    (hours,),
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in rows]
            finally:
                conn.close()
        else:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM frp_detections "
                    "WHERE acquisition_time >= datetime('now', ?) "
                    "ORDER BY acquisition_time ASC",
                    (f'-{hours} hours',),
                ).fetchall()
            return [dict(r) for r in rows]

    def count_frp_detections(self, hours: int = 24) -> int:
        if self._is_pg:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT COUNT(*) FROM frp_detections "
                    "WHERE CASE "
                    "  WHEN acquisition_time ~ '^[0-9]{14}$' "
                    "    THEN to_timestamp(acquisition_time, 'YYYYMMDDHH24MISS') "
                    "  ELSE acquisition_time::timestamp "
                    "END >= NOW() - make_interval(hours => %s)",
                    (hours,),
                )
                return cur.fetchone()[0]
            finally:
                conn.close()
        else:
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
        if self._is_pg:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM frp_detections "
                    "WHERE CASE "
                    "  WHEN acquisition_time ~ '^[0-9]{14}$' "
                    "    THEN to_timestamp(acquisition_time, 'YYYYMMDDHH24MISS') "
                    "  ELSE acquisition_time::timestamp "
                    "END >= NOW() - make_interval(hours => %s) "
                    "AND latitude BETWEEN %s AND %s "
                    "AND longitude BETWEEN %s AND %s "
                    "AND confidence >= %s "
                    "ORDER BY acquisition_time ASC",
                    (hours, lat_min, lat_max, lon_min, lon_max, min_confidence),
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in rows]
            finally:
                conn.close()
        else:
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
