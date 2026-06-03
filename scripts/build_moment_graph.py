from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bucket_manager import BucketManager
from memory_layers import can_moment_be_direct_seed, can_moment_be_related_target
from memory_moments import MemoryMomentStore, parse_bucket_moments
from memory_relevance import MemoryRelevanceOptions, content_terms_for_query, memory_relevance_options_from_config
from utils import load_config


GENERATED_REASON_PREFIX = "local_graph:"
DEFAULT_STATE_NAME = "moment_graph_worker.json"
WEAK_TERMS = {
    "todo",
    "done",
    "wish",
    "commitment",
    "emotional_echo",
    "project_event",
    "relationship_event",
    "family_milestone",
    "task_status_signal",
    "old_or_resolved",
    "resolved",
    "digested",
    "archive",
    "favorite",
    "haven_favorite",
    "记忆",
    "回忆",
    "上下文",
    "最近",
    "之前",
    "现在",
    "当前",
    "事情",
    "状态",
    "相关",
    "内容",
    "memory",
    "context",
    "recent",
    "status",
}
WEAK_TERM_PREFIXES = ("flavor_",)
WEAK_GRAPH_FACETS = {"old_or_resolved"}
CONTEXT_GLUE_TERMS = {
    "与",
    "和",
    "跟",
    "告诉",
    "告诉我",
    "说",
    "说过",
    "让我",
    "让",
    "对我",
    "向我",
    "给我",
}


@dataclass(frozen=True)
class IndexedMoment:
    moment: dict[str, Any]
    terms: set[str]
    facets: set[str]
    tags: set[str]
    domains: set[str]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    config = load_config()
    default_state = Path(config["state_dir"]) / DEFAULT_STATE_NAME
    parser = argparse.ArgumentParser(
        description="Build local cross-bucket moment graph edges without blocking recall requests."
    )
    parser.add_argument("--incremental", action="store_true", help="Skip work when bucket signatures did not change.")
    parser.add_argument("--write", action="store_true", help="Write generated local_graph edges. Default is dry-run.")
    parser.add_argument("--force", action="store_true", help="Run even when --incremental sees no changes.")
    parser.add_argument("--state-file", default=os.environ.get("OMBRE_MOMENT_GRAPH_STATE", str(default_state)))
    parser.add_argument("--min-score", type=float, default=0.58)
    parser.add_argument("--max-edges-per-moment", type=int, default=3)
    parser.add_argument("--max-moments", type=int, default=2000)
    return parser.parse_args(argv)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"bucket_signatures": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"bucket_signatures": {}}
    if not isinstance(data, dict):
        return {"bucket_signatures": {}}
    signatures = data.get("bucket_signatures")
    if not isinstance(signatures, dict):
        data["bucket_signatures"] = {}
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def bucket_signature(bucket: dict[str, Any]) -> str:
    meta = bucket.get("metadata", {}) if isinstance(bucket.get("metadata"), dict) else {}
    payload = {
        "id": bucket.get("id") or meta.get("id"),
        "content": bucket.get("content") or "",
        "name": meta.get("name"),
        "tags": meta.get("tags"),
        "domain": meta.get("domain"),
        "updated_at": meta.get("updated_at"),
        "last_active": meta.get("last_active"),
        "comments": meta.get("comments"),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def changed_bucket_ids(buckets: list[dict[str, Any]], state: dict[str, Any]) -> list[str]:
    old = state.get("bucket_signatures", {}) if isinstance(state.get("bucket_signatures"), dict) else {}
    changed = []
    for bucket in buckets:
        bucket_id = str(bucket.get("id") or "").strip()
        if not bucket_id:
            continue
        if old.get(bucket_id) != bucket_signature(bucket):
            changed.append(bucket_id)
    removed = set(old) - {str(bucket.get("id") or "") for bucket in buckets}
    return sorted(set(changed) | removed)


def state_for_buckets(buckets: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "bucket_signatures": {
            str(bucket.get("id")): bucket_signature(bucket)
            for bucket in buckets
            if bucket.get("id")
        },
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def parse_moments_for_dry_run(store: MemoryMomentStore, buckets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    moments = []
    for bucket in buckets:
        moments.extend(parse_bucket_moments(bucket, store.relevance_options, store.annotation_options))
    return moments


def index_moments(
    moments: list[dict[str, Any]],
    options: MemoryRelevanceOptions,
    *,
    max_moments: int,
) -> list[IndexedMoment]:
    indexed = []
    for moment in moments[: max(1, int(max_moments))]:
        if not moment.get("moment_id") or not moment.get("bucket_id"):
            continue
        terms = moment_terms(moment, options)
        facets = moment_facets(moment)
        tags = metadata_set(moment, "bucket_tags", options)
        domains = metadata_set(moment, "bucket_domain", options)
        if not terms and not facets:
            continue
        indexed.append(IndexedMoment(moment, terms, facets, tags, domains))
    return indexed


def build_cross_bucket_edges(
    moments: list[dict[str, Any]],
    options: MemoryRelevanceOptions | None = None,
    *,
    min_score: float = 0.58,
    max_edges_per_moment: int = 3,
    max_moments: int = 2000,
) -> list[dict[str, Any]]:
    options = options or memory_relevance_options_from_config()
    indexed = index_moments(moments, options, max_moments=max_moments)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    outgoing: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    for source in indexed:
        if not can_moment_be_direct_seed(source.moment):
            continue
        for target in indexed:
            if source.moment["bucket_id"] == target.moment["bucket_id"]:
                continue
            if not can_moment_be_related_target(target.moment):
                continue
            score, reason_bits = pair_score(source, target)
            if score < min_score:
                continue
            edge = {
                "source": source.moment["moment_id"],
                "target": target.moment["moment_id"],
                "bucket_id": source.moment["bucket_id"],
                "relation_type": relation_type_for(score, source, target),
                "confidence": round(min(0.95, max(0.0, score)), 3),
                "reason": f"{GENERATED_REASON_PREFIX}{'; '.join(reason_bits)}",
                "created_at": now,
            }
            outgoing.setdefault(source.moment["moment_id"], []).append((score, edge))

    edges = []
    for candidates in outgoing.values():
        candidates.sort(key=lambda item: item[0], reverse=True)
        edges.extend(edge for _score, edge in candidates[: max(1, int(max_edges_per_moment))])
    return dedupe_edges(edges)


def pair_score(source: IndexedMoment, target: IndexedMoment) -> tuple[float, list[str]]:
    score = 0.0
    reason = []
    term_overlap = source.terms & target.terms
    if not has_term_evidence(term_overlap):
        return 0.0, []
    if term_overlap:
        term_score = min(0.46, len(term_overlap) / math.sqrt(max(1, len(source.terms) * len(target.terms))))
        score += 0.24 + term_score
        reason.append("terms=" + ",".join(sorted(term_overlap)[:5]))
    facet_overlap = source.facets & target.facets
    if facet_overlap:
        score += min(0.28, 0.16 + 0.06 * len(facet_overlap))
        reason.append("facets=" + ",".join(sorted(facet_overlap)[:5]))
    tag_overlap = source.tags & target.tags
    if tag_overlap:
        score += min(0.1, 0.04 + 0.02 * len(tag_overlap))
        reason.append("tags=" + ",".join(sorted(tag_overlap)[:4]))
    domain_overlap = source.domains & target.domains
    if domain_overlap:
        score += min(0.06, 0.03 + 0.01 * len(domain_overlap))
        reason.append("domains=" + ",".join(sorted(domain_overlap)[:3]))
    if preferred_section(source.moment) and preferred_section(target.moment):
        score += 0.04
    return round(score, 4), reason


def relation_type_for(score: float, source: IndexedMoment, target: IndexedMoment) -> str:
    term_overlap = source.terms & target.terms
    if score >= 0.82 and source.facets & target.facets and len(term_overlap) >= 2:
        return "same_event"
    if source.facets & target.facets:
        return "context_of"
    return "supports"


def moment_terms(moment: dict[str, Any], options: MemoryRelevanceOptions) -> set[str]:
    meta = moment.get("metadata", {}) if isinstance(moment.get("metadata"), dict) else {}
    fields = " ".join(
        [
            str(moment.get("text") or ""),
            str(meta.get("annotation_summary") or ""),
            str(meta.get("bucket_name") or ""),
        ]
    )
    terms = content_terms_for_query(fields, options)
    return {
        normalize_term(term)
        for term in terms
        if keep_term(term) and not is_context_glue_term(term, options.context_terms)
    }


def moment_facets(moment: dict[str, Any]) -> set[str]:
    meta = moment.get("metadata", {}) if isinstance(moment.get("metadata"), dict) else {}
    raw = meta.get("annotation_facets")
    if not isinstance(raw, dict):
        return set()
    facets = set()
    for facet, value in raw.items():
        try:
            score = float(value)
        except (TypeError, ValueError):
            continue
        facet_name = str(facet)
        if facet_name in WEAK_GRAPH_FACETS:
            continue
        if score >= 0.35:
            facets.add(facet_name)
    return facets


def metadata_set(
    moment: dict[str, Any],
    key: str,
    options: MemoryRelevanceOptions,
) -> set[str]:
    meta = moment.get("metadata", {}) if isinstance(moment.get("metadata"), dict) else {}
    value = meta.get(key)
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = []
    return {
        normalize_term(item)
        for item in items
        if keep_metadata_term(item) and not is_context_glue_term(item, options.context_terms)
    }


def preferred_section(moment: dict[str, Any]) -> bool:
    return str(moment.get("section") or "") in {"body", "original", "moment", "fact", "context", "evidence_context"}


def keep_term(value: Any) -> bool:
    term = normalize_term(value)
    if not term or term in WEAK_TERMS:
        return False
    if any(term.startswith(prefix) for prefix in WEAK_TERM_PREFIXES):
        return False
    if not re.search(r"[a-zA-Z\u4e00-\u9fff]", term):
        return False
    if re.fullmatch(r"0x[0-9a-f]+", term):
        return False
    if re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", term):
        return False
    if re.fullmatch(r"[a-z0-9_:-]+", term) and len(term) < 3:
        return False
    if re.fullmatch(r"[\u4e00-\u9fff]+", term) and len(term) < 2:
        return False
    return True


def keep_metadata_term(value: Any) -> bool:
    term = normalize_term(value)
    if not keep_term(term):
        return False
    if term in WEAK_TERMS:
        return False
    if any(term.startswith(prefix) for prefix in WEAK_TERM_PREFIXES):
        return False
    return True


def has_term_evidence(terms: set[str]) -> bool:
    if len(terms) >= 2:
        return True
    return any(is_anchor_term(term) for term in terms)


def is_anchor_term(term: str) -> bool:
    value = normalize_term(term)
    if not keep_term(value):
        return False
    if re.search(r"\d", value):
        return True
    if re.fullmatch(r"[a-z][a-z0-9_-]{2,}", value):
        return True
    if re.fullmatch(r"[\u4e00-\u9fff]{3,}", value):
        return True
    return False


def is_context_glue_term(term: Any, context_terms: tuple[str, ...]) -> bool:
    value = normalize_term(term)
    if not value:
        return False
    for context_term in context_terms or ():
        context = normalize_term(context_term)
        if not context or context not in value:
            continue
        residue = value.replace(context, "")
        if not residue or residue in CONTEXT_GLUE_TERMS or len(residue) <= 1:
            return True
    return False


def normalize_term(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def dedupe_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for edge in edges:
        key = (edge["source"], edge["target"], edge["relation_type"])
        existing = deduped.get(key)
        if not existing or float(edge.get("confidence", 0.0)) > float(existing.get("confidence", 0.0)):
            deduped[key] = edge
    return list(deduped.values())


async def run_once(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    bucket_mgr = BucketManager(config)
    store = MemoryMomentStore(config)
    buckets = await bucket_mgr.list_all(include_archive=False)
    state_path = Path(args.state_file)
    state = load_state(state_path)
    changed = changed_bucket_ids(buckets, state)
    if args.incremental and not changed and not args.force:
        return {
            "status": "idle",
            "dry_run": not args.write,
            "bucket_count": len(buckets),
            "changed_bucket_count": 0,
            "state_file": str(state_path),
        }

    if args.write:
        indexed = store.bulk_upsert(buckets)
        moments = store.list_all(limit=max(1, int(args.max_moments)))
    else:
        indexed = {"buckets": 0, "moments": 0}
        moments = parse_moments_for_dry_run(store, buckets)

    edges = build_cross_bucket_edges(
        moments,
        store.relevance_options,
        min_score=float(args.min_score),
        max_edges_per_moment=int(args.max_edges_per_moment),
        max_moments=int(args.max_moments),
    )
    written = 0
    if args.write:
        written = store.replace_generated_edges(edges, reason_prefix=GENERATED_REASON_PREFIX)
        save_state(state_path, state_for_buckets(buckets))

    return {
        "status": "ok",
        "dry_run": not args.write,
        "bucket_count": len(buckets),
        "changed_bucket_count": len(changed),
        "indexed": indexed,
        "candidate_edge_count": len(edges),
        "written_edge_count": written,
        "state_file": str(state_path),
        "sample_edges": edges[:10],
    }


def print_result(result: dict[str, Any]) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))


async def amain(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = await run_once(args)
    print_result(result)
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
