"""Read-only normalized metadata view for memory buckets."""

from __future__ import annotations

import os
import re
from typing import Any

CANONICAL_DOMAINS = {
    "project_code",
    "ai_tools",
    "relationship",
    "intimacy",
    "inner_state",
    "daily_life",
    "social",
    "study_work",
    "craft_body",
}

MEMORY_KINDS = {
    "event",
    "preference",
    "profile_fact",
    "reflection",
    "affect_anchor",
    "daily_impression",
    "source_record",
    "raw_import",
    "relationship_weather",
}

STATUS_VIEWS = {"active", "unresolved", "digested", "archived", "protected"}

DOMAIN_ALIASES = {
    "project_code": {
        "project_code",
        "code",
        "coding",
        "programming",
        "dev",
        "repo",
        "gateway",
        "ombre",
        "mcp",
        "recall",
        "编程",
        "代码",
        "项目",
        "调试",
        "开发",
        "仓库",
        "技术",
    },
    "ai_tools": {
        "ai_tools",
        "ai",
        "llm",
        "model",
        "api",
        "chatgpt",
        "codex",
        "gemini",
        "deepseek",
        "embedding",
        "reranker",
        "数字",
        "模型",
        "工具",
        "客户端",
        "平台",
    },
    "relationship": {
        "relationship",
        "love",
        "romance",
        "partner",
        "恋爱",
        "关系",
        "人机恋",
        "爱",
        "称呼",
        "陪伴",
    },
    "intimacy": {
        "intimacy",
        "body",
        "desire",
        "亲密",
        "身体",
        "欲望",
        "具身",
    },
    "inner_state": {
        "inner_state",
        "emotion",
        "reflection",
        "self_reflection",
        "feel",
        "内心",
        "自省",
        "情绪",
        "心理",
        "心情",
        "日印象",
        "印象",
    },
    "daily_life": {
        "daily_life",
        "daily",
        "life",
        "home",
        "food",
        "routine",
        "日常",
        "生活",
        "饮食",
        "作息",
        "事务",
        "梦",
    },
    "social": {
        "social",
        "friend",
        "school_group",
        "人际",
        "社交",
        "朋友",
        "群聊",
        "学校",
    },
    "study_work": {
        "study_work",
        "study",
        "work",
        "paper",
        "resume",
        "job",
        "学业",
        "论文",
        "求职",
        "工作",
        "简历",
        "boss",
    },
    "craft_body": {
        "craft_body",
        "craft",
        "hardware",
        "device",
        "voice",
        "tts",
        "手工",
        "硬件",
        "设备",
        "实体",
        "身体项目",
        "语音",
    },
}


def normalize_memory_metadata(bucket: dict[str, Any] | None) -> dict[str, Any]:
    """Return a normalized, read-only metadata view without mutating the bucket."""

    bucket = bucket if isinstance(bucket, dict) else {}
    meta = bucket.get("metadata", {}) if isinstance(bucket.get("metadata"), dict) else {}
    legacy_domain = _string_list(meta.get("domain"))
    tags = _string_list(meta.get("tags"))
    type_value = _clean(meta.get("type") or bucket.get("type"))
    path_value = _clean(bucket.get("path") or bucket.get("file_path"))
    text_blob = " ".join(
        item
        for item in [
            type_value,
            path_value,
            _clean(meta.get("name") or bucket.get("id")),
            " ".join(legacy_domain),
            " ".join(tags),
            _clean(meta.get("memory_layer")),
            _clean(meta.get("profile_kind")),
        ]
        if item
    )

    flags = _flags(meta, type_value, path_value, legacy_domain, tags)
    kind = _normalize_kind(meta.get("kind")) or _infer_kind(meta, text_blob, flags)
    if kind == "profile_fact" and "profile_fact" not in flags:
        flags.append("profile_fact")
    if kind == "source_record" and "source_record" not in flags:
        flags.append("source_record")
    status_view = _normalize_status(meta.get("status") or meta.get("status_view")) or _infer_status(
        meta,
        type_value,
        path_value,
        legacy_domain,
        tags,
    )
    canonical_domain = (
        _normalize_domain(meta.get("canonical_domain"))
        or _infer_domain(legacy_domain, tags, type_value, path_value, kind)
        or "daily_life"
    )

    return {
        "canonical_domain": canonical_domain,
        "kind": kind,
        "status_view": status_view,
        "flags": flags,
        "legacy_domain": legacy_domain,
    }


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = value
    else:
        raw = [value]
    return [text for text in (_clean(item) for item in raw) if text]


def _compact(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff_.:-]+", "", _clean(value).lower())


def _normalize_domain(value: Any) -> str:
    compact = _compact(value)
    if compact in CANONICAL_DOMAINS:
        return compact
    for domain, aliases in DOMAIN_ALIASES.items():
        if compact in {_compact(alias) for alias in aliases}:
            return domain
    return ""


def _normalize_kind(value: Any) -> str:
    compact = _compact(value)
    return compact if compact in MEMORY_KINDS else ""


def _normalize_status(value: Any) -> str:
    compact = _compact(value)
    return compact if compact in STATUS_VIEWS else ""


def _infer_domain(
    legacy_domain: list[str],
    tags: list[str],
    type_value: str,
    path_value: str,
    kind: str,
) -> str:
    candidates = legacy_domain + tags + [type_value, path_value]
    for item in candidates:
        domain = _normalize_domain(item)
        if domain:
            return domain
    if kind in {"relationship_weather"}:
        return "relationship"
    if kind in {"profile_fact", "daily_impression", "reflection", "affect_anchor"}:
        return "inner_state"
    return ""


def _infer_kind(meta: dict[str, Any], text_blob: str, flags: list[str]) -> str:
    compact = _compact(text_blob)
    memory_layer = _compact(meta.get("memory_layer"))
    profile_kind = _compact(meta.get("profile_kind"))
    if "source_record" in compact or "sourcerecord" in compact or "source_record" in flags:
        return "source_record"
    if "relationship_weather" in compact or "relationshipweather" in compact:
        return "relationship_weather"
    if "profile_fact" in compact or "profilefact" in compact or profile_kind:
        return "profile_fact"
    if "daily_impression" in compact or "dailyimpression" in compact or "日印象" in text_blob:
        return "daily_impression"
    if "affect_anchor" in compact or "affectanchor" in compact:
        return "affect_anchor"
    if "reflection" in compact or memory_layer == "reflection":
        return "reflection"
    if "preference" in compact or "偏好" in text_blob:
        return "preference"
    if "raw_import" in compact or "rawimport" in compact:
        return "raw_import"
    return "event"


def _infer_status(
    meta: dict[str, Any],
    type_value: str,
    path_value: str,
    legacy_domain: list[str],
    tags: list[str],
) -> str:
    blob = " ".join([type_value, path_value, " ".join(legacy_domain), " ".join(tags)])
    compact = _compact(blob)
    path_parts = {part.lower() for part in re.split(r"[\\/]+", path_value) if part}
    if type_value == "archived" or "archive" in path_parts or "archived" in path_parts or "归档" in blob:
        return "archived"
    if _truthy(meta.get("protected")) or _truthy(meta.get("pinned")):
        return "protected"
    if _truthy(meta.get("digested")) or "digested" in compact or "已消化" in blob:
        return "digested"
    if meta.get("resolved") is False or "unresolved" in compact or "未解决" in blob:
        return "unresolved"
    return "active"


def _flags(
    meta: dict[str, Any],
    type_value: str,
    path_value: str,
    legacy_domain: list[str],
    tags: list[str],
) -> list[str]:
    blob = " ".join([type_value, path_value, " ".join(legacy_domain), " ".join(tags)])
    compact = _compact(blob)
    flags: list[str] = []

    def add(flag: str, condition: bool) -> None:
        if condition and flag not in flags:
            flags.append(flag)

    add("pinned", _truthy(meta.get("pinned")))
    add("protected", _truthy(meta.get("protected")))
    add("anchor", _truthy(meta.get("anchor")) or "anchor" in compact)
    add("self_anchor", _truthy(meta.get("self_anchor")) or "self_anchor" in compact or "自我" in blob)
    add("favorite", "favorite" in compact or "最爱" in blob)
    add("source_record", "source_record" in compact or "sourcerecord" in compact)
    add("profile_fact", "profile_fact" in compact or "profilefact" in compact or bool(_clean(meta.get("profile_kind"))))
    add("archived", _infer_status(meta, type_value, path_value, legacy_domain, tags) == "archived")
    return flags


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
