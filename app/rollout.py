"""默认投递的灰度分桶：按规范化地址把流量稳定切分到基线 / 候选版本。

分桶只依赖规范化地址本身（SHA-256 取模 100），与策略修订号无关：
同一地址在同一策略下总选中同一版本；提高候选比例只会把地址从基线
单调迁移到候选（不会反向抖动），0% 全选基线、100% 全选候选。
"""
from __future__ import annotations

import hashlib

BUCKETS = 100  # 候选流量比例按整数百分比 0..100 对应到 100 个桶


def bucket_for(address: str) -> int:
    """规范化地址（已小写、去空白）→ 稳定桶号 0..99。"""
    digest = hashlib.sha256(address.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % BUCKETS


def pick_version(address: str, policy: dict) -> int:
    """按候选流量比例在基线 / 候选版本间选择：桶号小于比例即进候选，否则留在基线。"""
    if bucket_for(address) < policy["candidate_percent"]:
        return policy["candidate_version"]
    return policy["base_version"]
