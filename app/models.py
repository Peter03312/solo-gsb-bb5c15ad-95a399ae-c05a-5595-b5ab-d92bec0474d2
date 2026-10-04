"""请求模型与草稿规则的编辑期校验（无效地址 / 重复规则 / 空目标在此拒绝）。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .routing import Rule, Target, is_valid_address, is_valid_domain


class TargetIn(BaseModel):
    kind: Literal["alias", "mailbox"]
    value: str


class RuleIn(BaseModel):
    match_type: Literal["exact", "domain"]
    match_key: str
    action: Literal["reject", "route"]
    targets: list[TargetIn] = Field(default_factory=list)


class DraftUpdate(BaseModel):
    revision: int = Field(ge=0, description="客户端所见草稿修订号")
    rules: list[RuleIn]


class PublishRequest(BaseModel):
    revision: int = Field(ge=0, description="客户端所见草稿修订号")


class ResolveRequest(BaseModel):
    address: str
    version: int = Field(ge=1, description="已发布版本号")


class PreviewRequest(BaseModel):
    base_version: int = Field(ge=1, description="基线已发布版本号")
    revision: int = Field(ge=0, description="客户端所见草稿修订号")


class PolicyUpdate(BaseModel):
    baseline_version: int = Field(ge=1, description="基线已发布版本号")
    candidate_version: int = Field(ge=1, description="候选已发布版本号")
    candidate_percent: int = Field(ge=0, le=100, description="候选流量比例（整数 0–100）")
    revision: int = Field(ge=0, description="客户端所见策略修订号（未配置时为 0）")


class DeliverRequest(BaseModel):
    address: str


class ValidationFailure(Exception):
    """编辑期校验失败，message 直接作为 4xx 响应。"""


def normalize_rules(raw: list[RuleIn]) -> list[Rule]:
    """把请求里的规则规范化为内部 Rule，并做编辑期校验。

    - 键与目标统一小写、去空白；
    - 无效地址 / 域、重复规则、空目标（route 无目标、reject 带目标）一律拒绝。
    """
    rules: list[Rule] = []
    seen_keys: set[tuple[str, str]] = set()

    for item in raw:
        key = item.match_key.strip().lower()
        if item.match_type == "exact":
            if not is_valid_address(key):
                raise ValidationFailure(f"invalid address in exact rule: {item.match_key!r}")
        else:
            if not is_valid_domain(key):
                raise ValidationFailure(f"invalid domain in domain rule: {item.match_key!r}")

        dedup = (item.match_type, key)
        if dedup in seen_keys:
            raise ValidationFailure(f"duplicate rule for {item.match_type} {key!r}")
        seen_keys.add(dedup)

        if item.action == "reject":
            if item.targets:
                raise ValidationFailure(f"reject rule for {key!r} must not have targets")
            targets: tuple[Target, ...] = ()
        else:
            if not item.targets:
                raise ValidationFailure(f"route rule for {key!r} has empty targets")
            normalized: list[Target] = []
            seen_targets: set[tuple[str, str]] = set()
            for t in item.targets:
                value = t.value.strip().lower()
                if not is_valid_address(value):
                    raise ValidationFailure(f"invalid target address {t.value!r} in rule for {key!r}")
                if (t.kind, value) not in seen_targets:
                    seen_targets.add((t.kind, value))
                    normalized.append(Target(kind=t.kind, value=value))
            targets = tuple(normalized)

        rules.append(Rule(match_type=item.match_type, match_key=key, action=item.action, targets=targets))

    return rules
