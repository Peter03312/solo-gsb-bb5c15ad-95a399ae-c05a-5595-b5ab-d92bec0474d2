"""SQLite 存储层：草稿（带乐观锁修订号）与不可变发布版本。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS draft_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    revision INTEGER NOT NULL
);
INSERT OR IGNORE INTO draft_meta (id, revision) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS draft_rules (
    match_type TEXT NOT NULL CHECK (match_type IN ('exact', 'domain')),
    match_key  TEXT NOT NULL,
    action     TEXT NOT NULL CHECK (action IN ('reject', 'route')),
    targets    TEXT NOT NULL DEFAULT '[]',   -- JSON: [{"kind": "alias"|"mailbox", "value": "..."}]
    PRIMARY KEY (match_type, match_key)
);

CREATE TABLE IF NOT EXISTS versions (
    version         INTEGER PRIMARY KEY AUTOINCREMENT,
    source_revision INTEGER NOT NULL,        -- 发布时核对通过的草稿修订号
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS version_rules (
    version    INTEGER NOT NULL REFERENCES versions (version),
    match_type TEXT NOT NULL,
    match_key  TEXT NOT NULL,
    action     TEXT NOT NULL,
    targets    TEXT NOT NULL,
    PRIMARY KEY (version, match_type, match_key)
);

-- 默认投递策略：单行（id 恒为 1）。未配置时行不存在，策略修订号视为 0。
-- 仅引用已发布版本；版本不可变、不可删除，外键长期有效。
CREATE TABLE IF NOT EXISTS delivery_policy (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    revision          INTEGER NOT NULL,                 -- 策略修订号，每次成功更新 +1
    baseline_version  INTEGER NOT NULL REFERENCES versions (version),
    candidate_version INTEGER NOT NULL REFERENCES versions (version),
    candidate_percent INTEGER NOT NULL CHECK (candidate_percent BETWEEN 0 AND 100),
    updated_at        TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, isolation_level=None)  # 自动提交，事务显式控制
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    directory = os.path.dirname(os.path.abspath(config.DB_PATH))
    os.makedirs(directory, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction():
    """BEGIN IMMEDIATE 事务：进入即取写锁，保证“核对修订号 + 写入”原子完成。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def read_transaction():
    """只读事务：同一快照内完成多次读取（如草稿 + 基线版本），期间不受并发写影响。"""
    conn = connect()
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
