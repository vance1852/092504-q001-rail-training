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
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workstations (
    workstation_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inspection_items (
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    item_code TEXT NOT NULL,
    title TEXT NOT NULL,
    system_code TEXT NOT NULL,
    safety_critical INTEGER NOT NULL CHECK(safety_critical IN (0, 1)),
    unit TEXT NOT NULL DEFAULT '',
    lower_limit REAL,
    upper_limit REAL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(organization_id, item_code)
);
CREATE TABLE IF NOT EXISTS measurements (
    measurement_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL REFERENCES vehicles(vehicle_id),
    workstation_id TEXT NOT NULL REFERENCES workstations(workstation_id),
    item_code TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    in_tolerance INTEGER NOT NULL CHECK(in_tolerance IN (0, 1)),
    component_batch_id TEXT REFERENCES component_batches(batch_id),
    measured_by TEXT NOT NULL REFERENCES actors(actor_id),
    measured_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_measurements_vehicle ON measurements(vehicle_id);
CREATE TABLE IF NOT EXISTS component_batches (
    batch_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    part_number TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    quantity INTEGER NOT NULL CHECK(quantity >= 0),
    location TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS component_movements (
    movement_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES component_batches(batch_id),
    action TEXT NOT NULL,
    from_location TEXT NOT NULL DEFAULT '',
    to_location TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    note TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_component_movements_batch ON component_movements(batch_id, occurred_at);
CREATE TABLE IF NOT EXISTS defect_cases (
    case_id TEXT PRIMARY KEY,
    case_key TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL REFERENCES vehicles(vehicle_id),
    workstation_id TEXT NOT NULL REFERENCES workstations(workstation_id),
    item_code TEXT NOT NULL,
    safety_critical INTEGER NOT NULL CHECK(safety_critical IN (0, 1)),
    source_measurement_id TEXT,
    status TEXT NOT NULL,
    current_revision_no INTEGER NOT NULL DEFAULT 0 CHECK(current_revision_no >= 0),
    lease_holder_id TEXT,
    lease_expires_at TEXT,
    opened_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(vehicle_id, workstation_id, case_key)
);
CREATE INDEX IF NOT EXISTS idx_defect_cases_status ON defect_cases(status);
CREATE TABLE IF NOT EXISTS case_revisions (
    revision_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES defect_cases(case_id),
    revision_no INTEGER NOT NULL CHECK(revision_no >= 1),
    diagnosis TEXT NOT NULL,
    isolation TEXT NOT NULL,
    retest_result TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    submitted_by TEXT NOT NULL REFERENCES actors(actor_id),
    submitted_at TEXT NOT NULL,
    UNIQUE(case_id, revision_no)
);
CREATE TABLE IF NOT EXISTS case_reviews (
    review_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES defect_cases(case_id),
    revision_no INTEGER NOT NULL,
    reviewer_id TEXT NOT NULL REFERENCES actors(actor_id),
    decision TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    signed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_case_reviews_case ON case_reviews(case_id, revision_no);
CREATE TABLE IF NOT EXISTS case_leases (
    lease_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES defect_cases(case_id),
    revision_base_no INTEGER NOT NULL,
    holder_id TEXT NOT NULL REFERENCES actors(actor_id),
    granted_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    released_at TEXT,
    release_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_case_leases_open ON case_leases(case_id) WHERE released_at IS NULL;
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        # 单连接跨线程复用时，用可重入锁串行化写入事务。
        self.lock = threading.RLock()
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        with self.lock:
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
