"""HTTP API：草稿编辑（乐观锁）、发布（原子校验 + 不可变版本）、按版本解析、发布前预览、默认投递策略。"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from . import db, preview, store
from .models import (
    DeliverRequest,
    DraftUpdate,
    PolicyUpdate,
    PreviewRequest,
    PublishRequest,
    ResolveRequest,
    ValidationFailure,
    normalize_rules,
)
from .routing import ResolutionError, RuleSet, is_valid_address, resolve, validate_ruleset
from .traffic import select_version


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()  # 首次启动自动建表
    yield


app = FastAPI(title="Alias Router", version="1.0.0", lifespan=lifespan)


def _conflict(exc: store.RevisionConflict) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"error": "revision_conflict", "current_revision": exc.current, "message": str(exc)},
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/draft")
def get_draft() -> dict:
    with db.connect() as conn:
        revision, rules = store.get_draft(conn)
    return {"revision": revision, "rules": [store.rule_to_dict(r) for r in rules]}


@app.put("/draft")
def put_draft(body: DraftUpdate) -> dict:
    """整体替换草稿。必须携带所见修订号，冲突拒绝（409）。"""
    try:
        rules = normalize_rules(body.rules)
    except ValidationFailure as exc:
        raise HTTPException(status_code=422, detail={"error": "invalid_rule", "message": str(exc)})
    try:
        with db.transaction() as conn:
            new_revision = store.replace_draft(conn, body.revision, rules)
    except store.RevisionConflict as exc:
        raise _conflict(exc)
    except sqlite3.OperationalError as exc:  # 并发写冲突
        raise HTTPException(status_code=409, detail={"error": "write_conflict", "message": str(exc)})
    return {"revision": new_revision, "rules": [store.rule_to_dict(r) for r in rules]}


@app.post("/publish")
def publish(body: PublishRequest) -> dict:
    """原子核对修订号并校验全部规则目标可解析且无环；成功生成不可变版本，失败保持旧版。"""
    try:
        with db.transaction() as conn:
            revision, rules = store.get_draft(conn)
            if revision != body.revision:
                raise store.RevisionConflict(revision)
            problems = validate_ruleset(RuleSet(rules))
            if problems:
                raise HTTPException(
                    status_code=422,
                    detail={"error": "validation_failed", "message": "unresolvable targets or routing loops", "problems": problems},
                )
            version = store.publish(conn, body.revision, rules)
    except store.RevisionConflict as exc:
        raise _conflict(exc)
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=409, detail={"error": "write_conflict", "message": str(exc)})
    return {"version": version, "source_revision": body.revision, "rule_count": len(rules)}


@app.get("/versions")
def list_versions() -> dict:
    with db.connect() as conn:
        return {"versions": store.list_versions(conn)}


@app.get("/versions/{version}")
def get_version(version: int) -> dict:
    with db.connect() as conn:
        found = store.get_version(conn, version)
    if found is None:
        raise HTTPException(status_code=404, detail={"error": "version_not_found", "version": version})
    return {
        "version": found["version"],
        "source_revision": found["source_revision"],
        "created_at": found["created_at"],
        "rules": [store.rule_to_dict(r) for r in found["rules"]],
    }


@app.post("/resolve")
def resolve_address(body: ResolveRequest) -> dict:
    """按指定已发布版本解析收件地址。任一分支拒收 / 无路由 / 成环则整次失败。"""
    address = body.address.strip().lower()
    if not is_valid_address(address):
        raise HTTPException(status_code=422, detail={"error": "invalid_address", "message": f"invalid address: {body.address!r}"})
    with db.connect() as conn:
        ruleset = store.load_ruleset(conn, body.version)
    if ruleset is None:
        raise HTTPException(status_code=404, detail={"error": "version_not_found", "version": body.version})
    try:
        endpoints = resolve(address, ruleset)
    except ResolutionError as exc:
        raise HTTPException(status_code=422, detail=exc.detail())
    return {
        "address": address,
        "version": body.version,
        "endpoints": [
            {"mailbox": mailbox, "paths": sorted(paths)}
            for mailbox, paths in sorted(endpoints.items())
        ],
    }


@app.post("/preview")
def preview_changes(body: PreviewRequest) -> dict:
    """发布前预览：当前草稿相对基线版本的投递变化。

    在同一只读事务快照内读取基线版本与草稿（不改动草稿或历史版本），
    对两侧全部精确规则地址与别名目标逐一解析，并对两侧出现过域规则的每个域
    用合成示例地址探测兜底类别；仅列出结果有变化的项，附两侧完整结果。
    草稿尚未通过发布校验时仍正常展示相应失败结果。
    """
    try:
        with db.read_transaction() as conn:
            revision, draft_rules = store.get_draft(conn)
            if revision != body.revision:
                raise store.RevisionConflict(revision)
            found = store.get_version(conn, body.base_version)
            if found is None:
                raise HTTPException(
                    status_code=404,
                    detail={"error": "version_not_found", "version": body.base_version},
                )
            base_rules = found["rules"]
    except store.RevisionConflict as exc:
        raise _conflict(exc)
    diff = preview.preview(base_rules, draft_rules)
    return {"base_version": body.base_version, "revision": revision, **diff}


# ---------- 默认投递策略（已发布版本间的灰度分桶） ----------

def _policy_doc(policy: dict) -> dict:
    return {
        "revision": policy["revision"],
        "baseline_version": policy["baseline_version"],
        "candidate_version": policy["candidate_version"],
        "candidate_percent": policy["candidate_percent"],
        "updated_at": policy["updated_at"],
    }


@app.get("/policy")
def get_policy() -> dict:
    """查看当前默认投递策略。从未配置时返回 404（策略修订号视为 0）。"""
    with db.read_transaction() as conn:
        policy = store.get_policy(conn)
    if policy is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "policy_not_configured", "revision": 0,
                    "message": "no delivery policy configured"},
        )
    return _policy_doc(policy)


@app.put("/policy")
def put_policy(body: PolicyUpdate) -> dict:
    """配置默认投递策略：基线 / 候选均须为已发布版本。

    在单个 BEGIN IMMEDIATE 事务内核验所见策略修订号并保存；
    修订号冲突（409）或引用未发布版本（422）时回滚，原策略不被覆盖。
    草稿与历史版本均不会因策略变更而改写。
    """
    try:
        with db.transaction() as conn:
            policy = store.replace_policy(
                conn,
                body.revision,
                body.baseline_version,
                body.candidate_version,
                body.candidate_percent,
            )
    except store.RevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "revision_conflict", "current_revision": exc.current, "message": str(exc)},
        )
    except store.PolicyVersionNotFound as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "version_not_published", "version": exc.version,
                    "message": f"only published versions may be used in a delivery policy: {exc.version}"},
        )
    except sqlite3.OperationalError as exc:  # 并发写冲突
        raise HTTPException(status_code=409, detail={"error": "write_conflict", "message": str(exc)})
    return _policy_doc(policy)


@app.post("/deliver")
def deliver(body: DeliverRequest) -> dict:
    """默认投递：按当前策略与规范化地址稳定分桶，选择基线或候选已发布版本解析。

    未配置策略时明确报错（409），不使用草稿或最新版本兜底；
    同一地址在同一策略下总选同一版本；选中版本的拒收 / 无路由 / 成环
    仍整次失败（422），不自动改用另一版本。
    """
    address = body.address.strip().lower()
    if not is_valid_address(address):
        raise HTTPException(status_code=422, detail={"error": "invalid_address", "message": f"invalid address: {body.address!r}"})
    with db.read_transaction() as conn:
        policy = store.get_policy(conn)
        if policy is None:
            raise HTTPException(
                status_code=409,
                detail={"error": "policy_not_configured",
                        "message": "no delivery policy configured; specify an explicit version via /resolve"},
            )
        chosen = select_version(address, policy["candidate_percent"])
        version = policy["baseline_version"] if chosen == "baseline" else policy["candidate_version"]
        ruleset = store.load_ruleset(conn, version)
    # 策略引用的版本必然已发布（写入时有外键 + 发布版本不可变），此处仅作防御
    if ruleset is None:
        raise HTTPException(status_code=404, detail={"error": "version_not_found", "version": version})
    try:
        endpoints = resolve(address, ruleset)
    except ResolutionError as exc:
        detail = exc.detail()
        detail.update({"policy_revision": policy["revision"], "version": version, "selection": chosen})
        raise HTTPException(status_code=422, detail=detail)
    return {
        "address": address,
        "policy_revision": policy["revision"],
        "baseline_version": policy["baseline_version"],
        "candidate_version": policy["candidate_version"],
        "candidate_percent": policy["candidate_percent"],
        "selection": chosen,
        "version": version,
        "endpoints": [
            {"mailbox": mailbox, "paths": sorted(paths)}
            for mailbox, paths in sorted(endpoints.items())
        ],
    }
