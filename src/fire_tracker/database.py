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
