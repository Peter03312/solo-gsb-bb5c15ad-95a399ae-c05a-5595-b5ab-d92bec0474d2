"""默认投递策略（PUT/GET /policy）与默认投递查询（POST /deliver）。

覆盖：策略只接受已发布版本、事务内核对修订号（冲突不覆盖）、按规范化地址稳定分桶、
0%/100% 边界、选中版本失败不自动改用另一版本、未配置策略明确报错、既有契约不受影响。
"""
import os
import tempfile

# 必须在导入 app 之前设置，config 在导入时读取
os.environ.setdefault("ROUTER_DB", os.path.join(tempfile.mkdtemp(prefix="router-test-"), "test.db"))

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.rollout import bucket_for, pick_version


@pytest.fixture()
def client():
    db.init_db()
    conn = db.connect()
    conn.executescript(
        "DELETE FROM draft_rules;"
        "DELETE FROM version_rules;"
        "DELETE FROM versions;"
        "DELETE FROM sqlite_sequence WHERE name = 'versions';"
        "UPDATE draft_meta SET revision = 0 WHERE id = 1;"
        "UPDATE policy SET revision = 0, base_version = NULL, candidate_version = NULL,"
        " candidate_percent = NULL WHERE id = 1;"
    )
    conn.close()
    with TestClient(app) as c:
        yield c


def team_rules(mailbox):
    """两版规则仅 team@ 的终点不同，便于区分命中的是哪个版本。"""
    return [
        {
            "match_type": "exact",
            "match_key": "team@corp.example.com",
            "action": "route",
            "targets": [{"kind": "mailbox", "value": mailbox}],
        },
        {
            "match_type": "domain",
            "match_key": "corp.example.com",
            "action": "route",
            "targets": [{"kind": "mailbox", "value": "catchall@mail.example.com"}],
        },
    ]


RULES_V1 = team_rules("alice@mail.example.com")
RULES_V2 = team_rules("carol@mail.example.com")


def put_rules(client, revision, rules):
    return client.put("/draft", json={"revision": revision, "rules": rules})


def publish(client, revision):
    return client.post("/publish", json={"revision": revision})


def setup_two_versions(client):
    assert put_rules(client, 0, RULES_V1).status_code == 200
    v1 = publish(client, 1).json()["version"]
    assert put_rules(client, 1, RULES_V2).status_code == 200
    v2 = publish(client, 2).json()["version"]
    return v1, v2


def set_policy(client, revision, base, candidate, percent):
    return client.put(
        "/policy",
        json={
            "revision": revision,
            "base_version": base,
            "candidate_version": candidate,
            "candidate_percent": percent,
        },
    )


def deliver(client, address):
    return client.post("/deliver", json={"address": address})


# ---------- 分桶单元：稳定、边界、单调 ----------

def test_bucket_is_stable_and_in_range():
    for i in range(200):
        address = f"user{i}@corp.example.com"
        assert 0 <= bucket_for(address) < 100
        assert bucket_for(address) == bucket_for(address)  # 同一地址桶号恒定


def test_pick_version_percent_boundaries():
    policy = {"base_version": 1, "candidate_version": 2, "candidate_percent": 0}
    assert all(pick_version(f"u{i}@x.example.com", policy) == 1 for i in range(200))
    policy["candidate_percent"] = 100
    assert all(pick_version(f"u{i}@x.example.com", policy) == 2 for i in range(200))


def test_pick_version_monotonic_in_percent():
    # 提高候选比例只会把地址从基线迁到候选，不会反向抖动
    policy = {"base_version": 1, "candidate_version": 2, "candidate_percent": 0}
    previous = {f"u{i}@x.example.com": 1 for i in range(200)}
    for percent in range(0, 101, 5):
        policy["candidate_percent"] = percent
        for address, old in previous.items():
            new = pick_version(address, policy)
            assert not (old == 2 and new == 1)
            previous[address] = new


# ---------- 策略配置：只接受已发布版本，事务内核对修订号 ----------

def test_policy_starts_unconfigured(client):
    resp = client.get("/policy")
    assert resp.status_code == 200
    assert resp.json() == {"revision": 0, "policy": None}


def test_put_policy_and_revision_increment(client):
    v1, v2 = setup_two_versions(client)
    resp = set_policy(client, 0, v1, v2, 25)
    assert resp.status_code == 200
    assert resp.json() == {
        "revision": 1,
        "policy": {"base_version": v1, "candidate_version": v2, "candidate_percent": 25},
    }
    assert client.get("/policy").json() == {
        "revision": 1,
        "policy": {"base_version": v1, "candidate_version": v2, "candidate_percent": 25},
    }
    # 用新修订号可继续更新（如调高比例）
    resp = set_policy(client, 1, v1, v2, 50)
    assert resp.status_code == 200
    assert resp.json()["revision"] == 2
    assert client.get("/policy").json()["policy"]["candidate_percent"] == 50


def test_put_policy_rejects_unpublished_versions(client):
    v1, _ = setup_two_versions(client)
    resp = set_policy(client, 0, v1, 99, 50)
    assert resp.status_code == 404
    assert resp.json()["detail"] == {"error": "version_not_found", "version": 99}
    resp = set_policy(client, 0, 98, v1, 50)
    assert resp.status_code == 404
    assert resp.json()["detail"]["version"] == 98
    # 未保存任何策略
    assert client.get("/policy").json() == {"revision": 0, "policy": None}


def test_put_policy_percent_out_of_range(client):
    v1, v2 = setup_two_versions(client)
    assert set_policy(client, 0, v1, v2, -1).status_code == 422
    assert set_policy(client, 0, v1, v2, 101).status_code == 422
    assert client.get("/policy").json() == {"revision": 0, "policy": None}


def test_put_policy_stale_revision_conflict_keeps_original(client):
    v1, v2 = setup_two_versions(client)
    assert set_policy(client, 0, v1, v2, 25).status_code == 200
    resp = set_policy(client, 0, v2, v1, 80)  # 旧修订号
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "revision_conflict"
    assert detail["current_revision"] == 1
    # 冲突不覆盖原策略
    assert client.get("/policy").json() == {
        "revision": 1,
        "policy": {"base_version": v1, "candidate_version": v2, "candidate_percent": 25},
    }


# ---------- 默认投递查询：分桶、稳定、边界 ----------

def test_deliver_without_policy_404(client):
    setup_two_versions(client)
    resp = deliver(client, "team@corp.example.com")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "policy_not_configured"


def test_deliver_zero_percent_all_baseline(client):
    v1, v2 = setup_two_versions(client)
    assert set_policy(client, 0, v1, v2, 0).status_code == 200
    resp = deliver(client, "team@corp.example.com")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == v1
    assert body["policy_revision"] == 1
    assert {e["mailbox"] for e in body["endpoints"]} == {"alice@mail.example.com"}


def test_deliver_hundred_percent_all_candidate(client):
    v1, v2 = setup_two_versions(client)
    assert set_policy(client, 0, v1, v2, 100).status_code == 200
    resp = deliver(client, "team@corp.example.com")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == v2
    assert {e["mailbox"] for e in body["endpoints"]} == {"carol@mail.example.com"}


def test_deliver_response_shape_and_paths(client):
    v1, v2 = setup_two_versions(client)
    assert set_policy(client, 0, v1, v2, 0).status_code == 200
    body = deliver(client, "team@corp.example.com").json()
    assert body["address"] == "team@corp.example.com"
    assert body["endpoints"] == [
        {"mailbox": "alice@mail.example.com", "paths": [["team@corp.example.com", "alice@mail.example.com"]]}
    ]


def test_deliver_split_matches_stable_bucket(client):
    v1, v2 = setup_two_versions(client)
    assert set_policy(client, 0, v1, v2, 50).status_code == 200
    policy = {"base_version": v1, "candidate_version": v2, "candidate_percent": 50}
    addresses = [f"user{i}@corp.example.com" for i in range(100)]
    seen_versions = set()
    for address in addresses:
        body = deliver(client, address).json()
        assert body["version"] == pick_version(address, policy)
        seen_versions.add(body["version"])
    assert seen_versions == {v1, v2}  # 样本足够，两侧都有命中


def test_deliver_same_address_same_policy_same_version(client):
    v1, v2 = setup_two_versions(client)
    assert set_policy(client, 0, v1, v2, 50).status_code == 200
    first = deliver(client, "someone@corp.example.com").json()
    for _ in range(3):
        again = deliver(client, "someone@corp.example.com").json()
        assert again["version"] == first["version"]
        assert again["endpoints"] == first["endpoints"]


def test_deliver_normalizes_address_before_bucketing(client):
    v1, v2 = setup_two_versions(client)
    assert set_policy(client, 0, v1, v2, 50).status_code == 200
    plain = deliver(client, "someone@corp.example.com").json()
    messy = deliver(client, "  Someone@Corp.Example.Com ").json()
    assert messy["address"] == "someone@corp.example.com"
    assert messy["version"] == plain["version"]


def test_deliver_policy_revision_in_response_tracks_policy(client):
    v1, v2 = setup_two_versions(client)
    assert set_policy(client, 0, v1, v2, 0).status_code == 200
    assert deliver(client, "team@corp.example.com").json()["policy_revision"] == 1
    assert set_policy(client, 1, v1, v2, 100).status_code == 200
    body = deliver(client, "team@corp.example.com").json()
    assert body["policy_revision"] == 2
    assert body["version"] == v2


def test_deliver_invalid_address_422(client):
    v1, v2 = setup_two_versions(client)
    assert set_policy(client, 0, v1, v2, 50).status_code == 200
    resp = deliver(client, "nope")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_address"


# ---------- 选中版本失败：整次失败，不自动改用另一版本 ----------

def test_deliver_reject_in_selected_candidate_fails_without_fallback(client):
    # 基线正常投递，候选拒收 team@；100% 时命中候选 → 整次 422，不回退基线
    v1, _ = setup_two_versions(client)
    rejecting = [
        {"match_type": "exact", "match_key": "team@corp.example.com", "action": "reject", "targets": []},
        {
            "match_type": "domain",
            "match_key": "corp.example.com",
            "action": "route",
            "targets": [{"kind": "mailbox", "value": "catchall@mail.example.com"}],
        },
    ]
    assert put_rules(client, 2, rejecting).status_code == 200
    v3 = publish(client, 3).json()["version"]
    assert set_policy(client, 0, v1, v3, 100).status_code == 200
    resp = deliver(client, "team@corp.example.com")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "rejected"
    assert "endpoints" not in resp.json()
    # 同一地址 0% 时命中基线则正常 —— 证明失败来自选中版本而非地址本身
    assert set_policy(client, 1, v1, v3, 0).status_code == 200
    assert deliver(client, "team@corp.example.com").status_code == 200


def test_deliver_no_route_in_selected_baseline_fails(client):
    # 基线删掉域兜底（未知地址无路由），候选正常；0% 命中基线 → 整次 422
    minimal = [
        {
            "match_type": "exact",
            "match_key": "team@corp.example.com",
            "action": "route",
            "targets": [{"kind": "mailbox", "value": "alice@mail.example.com"}],
        }
    ]
    assert put_rules(client, 0, minimal).status_code == 200
    v1 = publish(client, 1).json()["version"]
    assert put_rules(client, 1, RULES_V2).status_code == 200
    v2 = publish(client, 2).json()["version"]
    assert set_policy(client, 0, v1, v2, 0).status_code == 200
    resp = deliver(client, "ghost@corp.example.com")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "no_route"


# ---------- 兼容性：策略不影响草稿、历史版本与显式版本解析 ----------

def test_policy_change_does_not_touch_draft_or_versions(client):
    v1, v2 = setup_two_versions(client)
    assert set_policy(client, 0, v1, v2, 30).status_code == 200
    assert set_policy(client, 1, v2, v1, 70).status_code == 200
    # 草稿修订号与内容不变
    draft = client.get("/draft").json()
    assert draft["revision"] == 2
    assert len(draft["rules"]) == len(RULES_V2)
    # 历史版本不被改写
    assert [v["version"] for v in client.get("/versions").json()["versions"]] == [v1, v2]
    r1 = client.post("/resolve", json={"address": "team@corp.example.com", "version": v1}).json()
    assert {e["mailbox"] for e in r1["endpoints"]} == {"alice@mail.example.com"}
    r2 = client.post("/resolve", json={"address": "team@corp.example.com", "version": v2}).json()
    assert {e["mailbox"] for e in r2["endpoints"]} == {"carol@mail.example.com"}
    # 显式版本解析的错误约定不变
    resp = client.post("/resolve", json={"address": "team@corp.example.com", "version": 99})
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "version_not_found"
