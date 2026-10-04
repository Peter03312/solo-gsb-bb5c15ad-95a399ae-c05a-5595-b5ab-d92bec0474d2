"""持久层：草稿（带乐观锁修订号）、不可变发布版本与默认投递策略（基线/候选灰度）。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .routing import Rule, RuleSet, Target


class RevisionConflict(Exception):
    """所见修订号与当前修订号不一致（草稿或默认投递策略）。"""

    def __init__(self, current: int, subject: str = "draft"):
        super().__init__(f"revision conflict: current {subject} revision is {current}")
        self.current = current


class PolicyVersionNotFound(Exception):
    """策略引用了不存在的已发布版本。"""

    def __init__(self, version: int):
        super().__init__(f"published version not found: {version}")
        self.version = version


def _row_to_rule(row: sqlite3.Row) -> Rule:
    targets = tuple(Target(kind=t["kind"], value=t["value"]) for t in json.loads(row["targets"]))
    return Rule(
        match_type=row["match_type"],
        match_key=row["match_key"],
        action=row["action"],
        targets=targets,
    )


def rule_to_dict(rule: Rule) -> dict:
    return {
        "match_type": rule.match_type,
        "match_key": rule.match_key,
        "action": rule.action,
        "targets": [{"kind": t.kind, "value": t.value} for t in rule.targets],
    }


def get_draft(conn: sqlite3.Connection) -> tuple[int, list[Rule]]:
    revision = conn.execute("SELECT revision FROM draft_meta WHERE id = 1").fetchone()["revision"]
    rows = conn.execute(
        "SELECT match_type, match_key, action, targets FROM draft_rules ORDER BY match_type, match_key"
    ).fetchall()
    return revision, [_row_to_rule(r) for r in rows]


def replace_draft(conn: sqlite3.Connection, expected_revision: int, rules: list[Rule]) -> int:
    """整体替换草稿；修订号不匹配抛 RevisionConflict。须在事务内调用。"""
    revision, _ = get_draft(conn)
    if revision != expected_revision:
        raise RevisionConflict(revision)
    conn.execute("DELETE FROM draft_rules")
    conn.executemany(
        "INSERT INTO draft_rules (match_type, match_key, action, targets) VALUES (?, ?, ?, ?)",
        [
            (
                r.match_type,
                r.match_key,
                r.action,
                json.dumps([{"kind": t.kind, "value": t.value} for t in r.targets]),
            )
            for r in rules
        ],
    )
    new_revision = revision + 1
    conn.execute("UPDATE draft_meta SET revision = ? WHERE id = 1", (new_revision,))
    return new_revision


def publish(conn: sqlite3.Connection, expected_revision: int, rules: list[Rule]) -> int:
    """把当前草稿固化为不可变版本，返回版本号。须在事务内调用。"""
    revision, _ = get_draft(conn)
    if revision != expected_revision:
        raise RevisionConflict(revision)
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO versions (source_revision, created_at) VALUES (?, ?)",
        (revision, created_at),
    )
    version = cur.lastrowid
    conn.executemany(
        "INSERT INTO version_rules (version, match_type, match_key, action, targets) VALUES (?, ?, ?, ?, ?)",
        [
            (
                version,
                r.match_type,
                r.match_key,
                r.action,
                json.dumps([{"kind": t.kind, "value": t.value} for t in r.targets]),
            )
            for r in rules
        ],
    )
    return version


def list_versions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT v.version, v.source_revision, v.created_at, COUNT(r.match_key) AS rule_count
        FROM versions v LEFT JOIN version_rules r ON r.version = v.version
        GROUP BY v.version ORDER BY v.version
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_version(conn: sqlite3.Connection, version: int) -> dict | None:
    row = conn.execute(
        "SELECT version, source_revision, created_at FROM versions WHERE version = ?", (version,)
    ).fetchone()
    if row is None:
        return None
    rules = conn.execute(
        "SELECT match_type, match_key, action, targets FROM version_rules "
        "WHERE version = ? ORDER BY match_type, match_key",
        (version,),
    ).fetchall()
    return {**dict(row), "rules": [_row_to_rule(r) for r in rules]}


def load_ruleset(conn: sqlite3.Connection, version: int) -> RuleSet | None:
    found = get_version(conn, version)
    if found is None:
        return None
    return RuleSet(found["rules"])


# ---------- 默认投递策略（基线 / 候选灰度） ----------

def get_policy(conn: sqlite3.Connection) -> dict | None:
    """读取当前默认投递策略；从未配置时返回 None（修订号视为 0）。"""
    row = conn.execute(
        "SELECT revision, baseline_version, candidate_version, candidate_percent, updated_at "
        "FROM delivery_policy WHERE id = 1"
    ).fetchone()
    return dict(row) if row is not None else None


def replace_policy(
    conn: sqlite3.Connection,
    expected_revision: int,
    baseline_version: int,
    candidate_version: int,
    candidate_percent: int,
) -> dict:
    """在同一事务内核验所见策略修订号并保存新策略。

    - 未配置策略时修订号为 0，行尚不存在；
    - 修订号不匹配抛 RevisionConflict，调用方回滚，原策略保持不变；
    - 基线 / 候选必须都是已发布版本，否则抛 PolicyVersionNotFound（同样不改原策略）。
    """
    current = get_policy(conn)
    current_revision = current["revision"] if current is not None else 0
    if current_revision != expected_revision:
        raise RevisionConflict(current_revision, subject="policy")
    for version in (baseline_version, candidate_version):
        if conn.execute("SELECT 1 FROM versions WHERE version = ?", (version,)).fetchone() is None:
            raise PolicyVersionNotFound(version)
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO delivery_policy (id, revision, baseline_version, candidate_version, candidate_percent, updated_at)
        VALUES (1, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            revision = excluded.revision,
            baseline_version = excluded.baseline_version,
            candidate_version = excluded.candidate_version,
            candidate_percent = excluded.candidate_percent,
            updated_at = excluded.updated_at
        """,
        (current_revision + 1, baseline_version, candidate_version, candidate_percent, updated_at),
    )
    return {
        "revision": current_revision + 1,
        "baseline_version": baseline_version,
        "candidate_version": candidate_version,
        "candidate_percent": candidate_percent,
        "updated_at": updated_at,
    }
