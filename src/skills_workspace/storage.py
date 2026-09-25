"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stations (
    station_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS check_items (
    item_id TEXT PRIMARY KEY,
    system TEXT NOT NULL,
    title TEXT NOT NULL,
    safety_critical INTEGER NOT NULL CHECK(safety_critical IN (0, 1)),
    criteria TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS measurements (
    measurement_id TEXT PRIMARY KEY,
    vehicle_id TEXT NOT NULL REFERENCES vehicles(vehicle_id),
    item_id TEXT NOT NULL REFERENCES check_items(item_id),
    value TEXT NOT NULL,
    unit TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK(passed IN (0, 1)),
    payload_hash TEXT NOT NULL,
    measured_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS component_batches (
    batch_id TEXT PRIMARY KEY,
    part_number TEXT NOT NULL,
    description TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS defect_cases (
    case_id TEXT PRIMARY KEY,
    vehicle_id TEXT NOT NULL REFERENCES vehicles(vehicle_id),
    station_id TEXT NOT NULL REFERENCES stations(station_id),
    system TEXT NOT NULL,
    title TEXT NOT NULL,
    safety_critical INTEGER NOT NULL CHECK(safety_critical IN (0, 1)),
    status TEXT NOT NULL CHECK(status IN ('open','leased','in_review','awaiting_second_signature','closed')),
    version INTEGER NOT NULL CHECK(version >= 1),
    current_revision INTEGER NOT NULL CHECK(current_revision >= 0),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    closed_at TEXT,
    closed_revision INTEGER,
    closed_case_version INTEGER
);
CREATE TABLE IF NOT EXISTS case_items (
    case_id TEXT NOT NULL REFERENCES defect_cases(case_id),
    version INTEGER NOT NULL,
    item_id TEXT NOT NULL REFERENCES check_items(item_id),
    PRIMARY KEY(case_id, version, item_id)
);
CREATE TABLE IF NOT EXISTS case_measurements (
    case_id TEXT NOT NULL REFERENCES defect_cases(case_id),
    version INTEGER NOT NULL,
    measurement_id TEXT NOT NULL REFERENCES measurements(measurement_id),
    PRIMARY KEY(case_id, version, measurement_id)
);
CREATE TABLE IF NOT EXISTS case_revisions (
    case_id TEXT NOT NULL REFERENCES defect_cases(case_id),
    revision_no INTEGER NOT NULL,
    case_version INTEGER NOT NULL,
    submitted_by TEXT NOT NULL REFERENCES actors(actor_id),
    diagnosis TEXT NOT NULL,
    isolation TEXT NOT NULL,
    retest TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('in_review','rejected','awaiting_second_signature','closed')),
    review_note TEXT,
    reviewed_by TEXT,
    reviewed_at TEXT,
    second_note TEXT,
    second_signed_by TEXT,
    second_signed_at TEXT,
    submitted_at TEXT NOT NULL,
    PRIMARY KEY(case_id, revision_no)
);
CREATE TABLE IF NOT EXISTS leases (
    lease_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES defect_cases(case_id),
    trainee_id TEXT NOT NULL REFERENCES actors(actor_id),
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','expired','closed'))
);
CREATE UNIQUE INDEX IF NOT EXISTS leases_one_active_per_case ON leases(case_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS component_movements (
    movement_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES component_batches(batch_id),
    case_id TEXT NOT NULL REFERENCES defect_cases(case_id),
    revision_no INTEGER NOT NULL,
    vehicle_id TEXT NOT NULL REFERENCES vehicles(vehicle_id),
    action TEXT NOT NULL CHECK(action IN ('install','remove')),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    note TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)
        self._write_lock = threading.Lock()

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交；写事务按进程内锁串行执行。"""

        with self._write_lock:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
