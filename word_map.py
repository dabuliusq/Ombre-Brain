import itertools
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

import jieba
import jieba.analyse

from utils import now_iso, strip_affect_anchor, strip_wikilinks


DEFAULT_WORD_MAP_STOPWORDS = {
    "一个",
    "一些",
    "一下",
    "不是",
    "为了",
    "他们",
    "你们",
    "我们",
    "什么",
    "今天",
    "刚才",
    "刚刚",
    "这个",
    "那个",
    "这些",
    "那些",
    "然后",
    "现在",
    "自己",
    "because",
    "from",
    "have",
    "that",
    "this",
    "with",
    "you",
    "active",
    "anchor",
    "archive",
    "archived",
    "###",
    "##",
    "comment",
    "commitment",
    "context",
    "current",
    "daily_impression",
    "digested",
    "done",
    "dynamic",
    "emotional_echo",
    "event",
    "favorite",
    "feel",
    "haven_favorite",
    "memory",
    "moment",
    "original",
    "pending",
    "permanent",
    "profile_fact",
    "project_event",
    "recent",
    "relationship_weather",
    "relationship_event",
    "resolved",
    "status",
    "task_status_signal",
    "todo",
    "weekly_impression",
    "wish",
    "上下文",
    "事件",
    "内容",
    "回忆",
    "当前",
    "最近",
    "状态",
    "记忆",
}

DEFAULT_STOPWORD_PREFIXES = ("flavor_", "profile_", "predicate_", "task_")
DEFAULT_WORD_MAP_OVERVIEW_STOPWORDS = {
    "ai",
    "boundary_setting",
    "bucket_original",
    "communication_preference",
    "diary_extract",
    "from_diary",
    "interaction_pattern",
    "profile_preference",
    "两人",
    "主动",
    "亲密",
    "亲密互动",
    "互动模式",
    "人际",
    "关系",
    "关系天气",
    "内心",
    "兴趣",
    "回应",
    "喜欢",
    "命名日",
    "天气",
    "对话",
    "成长",
    "恋爱",
    "情感表达",
    "情感连接",
    "情绪",
    "承诺",
    "撒娇",
    "数字",
    "日印象",
    "日常",
    "仪式感",
    "凌晨",
    "甜蜜",
    "社交",
    "家庭",
    "编程",
    "自省",
    "陪伴",
    "归属感",
    "小乖",
    "birthday",
    "fact",
    "haven_chat_endpoint",
    "naming_day",
    "profile",
    "relationship_anchor",
    "ritual",
}
DEFAULT_OVERVIEW_STOPWORD_PREFIXES = DEFAULT_STOPWORD_PREFIXES
DEFAULT_OVERVIEW_ALIASES = {
    "darkroom": "暗房",
    "darkroom door": "暗房",
    "ombre": "Ombre-Brain",
    "ombre-brain": "Ombre-Brain",
    "ombre_brain": "Ombre-Brain",
    "mcp": "MCP",
    "dashboard": "Dashboard",
    "codex": "Codex",
}
DEFAULT_OVERVIEW_PRIORITY_TERMS = {
    "darkroom",
    "ombre-brain",
    "recall_cues",
    "暗房",
    "忱孚",
    "折角",
    "梦境机制",
    "流星",
    "第一行代码",
    "记忆不是表演",
}
DEFAULT_WEAK_HINT_TERMS = {
    "人机恋",
    "恋爱",
}
DEFAULT_WEAK_HINT_WEIGHT = 0.25


@dataclass(frozen=True)
class WordMapTerm:
    term: str
    source: str
    kind: str
    weight: float


class WordMapStore:
    """Derived word co-occurrence index for generic, non-private memory recall hints."""

    def __init__(self, config: dict[str, Any]):
        cfg = config.get("word_map", {}) if isinstance(config.get("word_map", {}), dict) else {}
        state_dir = str(config.get("state_dir") or os.path.join(config.get("buckets_dir", "."), "state"))
        self.enabled = bool(cfg.get("enabled", False))
        self.max_terms_per_bucket = _int_between(cfg.get("max_terms_per_bucket"), 16, 4, 80)
        self.edge_top_k = _int_between(cfg.get("edge_top_k"), 10, 2, 40)
        self.min_term_len = _int_between(cfg.get("min_term_len"), 2, 1, 12)
        self.weak_hint_weight = _float_between(
            cfg.get("weak_hint_weight"),
            DEFAULT_WEAK_HINT_WEIGHT,
            0.0,
            1.0,
        )
        self.db_path = str(cfg.get("db_path") or os.path.join(state_dir, "word_map.sqlite"))
        self.stopwords = {
            _normalize_term(item)
            for item in itertools.chain(
                DEFAULT_WORD_MAP_STOPWORDS,
                _identity_stopwords(config),
                cfg.get("stopwords", []) or [],
            )
            if _normalize_term(item)
        }
        self.stopword_prefixes = tuple(
            str(item).strip().lower()
            for item in itertools.chain(DEFAULT_STOPWORD_PREFIXES, cfg.get("stopword_prefixes", []) or [])
            if str(item).strip()
        )
        self.overview_stopwords = {
            _normalize_term(item)
            for item in itertools.chain(
                DEFAULT_WORD_MAP_OVERVIEW_STOPWORDS,
                cfg.get("overview_stopwords", []) or [],
            )
            if _normalize_term(item)
        }
        self.overview_stopword_prefixes = tuple(
            str(item).strip().lower()
            for item in itertools.chain(
                DEFAULT_OVERVIEW_STOPWORD_PREFIXES,
                cfg.get("overview_stopword_prefixes", []) or [],
            )
            if str(item).strip()
        )
        self.overview_aliases = {
            _normalize_term(key): str(value).strip()
            for key, value in itertools.chain(
                DEFAULT_OVERVIEW_ALIASES.items(),
                (cfg.get("overview_aliases", {}) or {}).items(),
            )
            if _normalize_term(key) and str(value).strip()
        }
        self.overview_priority_terms = {
            _normalize_term(item)
            for item in itertools.chain(DEFAULT_OVERVIEW_PRIORITY_TERMS, cfg.get("overview_priority_terms", []) or [])
            if _normalize_term(item)
        }
        self.weak_hint_terms = {
            _normalize_term(item)
            for item in itertools.chain(DEFAULT_WEAK_HINT_TERMS, cfg.get("weak_hint_terms", []) or [])
            if _normalize_term(item)
        }
        self.private_terms = {
            _normalize_term(item)
            for item in itertools.chain(
                cfg.get("private_terms", []) or [],
                reflection_identity_terms(config),
            )
            if _normalize_term(item)
        }
        self._init_db()

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS word_nodes (
                term TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                bucket_count INTEGER NOT NULL DEFAULT 0,
                weight REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS word_card_nodes (
                bucket_id TEXT NOT NULL,
                term TEXT NOT NULL,
                source TEXT NOT NULL,
                kind TEXT NOT NULL,
                weight REAL NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(bucket_id, term)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS word_edges (
                term_a TEXT NOT NULL,
                term_b TEXT NOT NULL,
                bucket_id TEXT NOT NULL,
                weight REAL NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(term_a, term_b, bucket_id)
            )
            """
        )
        conn.commit()
        conn.close()

    def rebuild(self, buckets: list[dict[str, Any]]) -> dict[str, int]:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM word_edges")
            conn.execute("DELETE FROM word_card_nodes")
            conn.execute("DELETE FROM word_nodes")
            for bucket in buckets:
                self._write_bucket(conn, bucket)
            self._refresh_node_stats(conn)
            conn.commit()
        finally:
            conn.close()
        return self.stats()

    def upsert_bucket(self, bucket: dict[str, Any]) -> dict[str, int]:
        bucket_id = str(bucket.get("id") or "").strip()
        if not bucket_id:
            return self.stats()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM word_edges WHERE bucket_id = ?", (bucket_id,))
            conn.execute("DELETE FROM word_card_nodes WHERE bucket_id = ?", (bucket_id,))
            self._write_bucket(conn, bucket)
            self._refresh_node_stats(conn)
            conn.commit()
        finally:
            conn.close()
        return self.stats()

    def extract_bucket_terms(self, bucket: dict[str, Any]) -> list[WordMapTerm]:
        meta = bucket.get("metadata", {}) if isinstance(bucket.get("metadata"), dict) else {}
        text = _bucket_text(bucket)
        terms: dict[str, WordMapTerm] = {}

        def add(raw: Any, source: str, kind: str, weight: float) -> None:
            term = self._clean_term(raw)
            if not term:
                return
            current = terms.get(term)
            incoming = WordMapTerm(term=term, source=source, kind=kind, weight=weight)
            if current is None or incoming.weight > current.weight:
                terms[term] = incoming

        for raw in _list_text(meta.get("subject")):
            add(raw, "subject", "subject", 1.0)
        name = str(meta.get("name") or "").strip()
        if 2 <= len(name) <= 32:
            add(name, "name", "subject", 0.9)
            jieba.add_word(name, freq=20000)
            for term in self._title_concept_terms(name):
                add(term, "title_keyword", "keyword", 0.94)
                jieba.add_word(term, freq=22000)
        for raw in _list_text(meta.get("keywords")):
            add(raw, "keyword", "keyword", 0.86)
            jieba.add_word(str(raw), freq=20000)
        for raw in _list_text(meta.get("tags")):
            add(raw, "tag", "keyword", 0.78)
            jieba.add_word(str(raw), freq=16000)
        for raw in _list_text(meta.get("domain")):
            add(raw, "domain", "keyword", 0.62)
            jieba.add_word(str(raw), freq=12000)

        for word, score in jieba.analyse.extract_tags(text, topK=max(self.max_terms_per_bucket * 2, 20), withWeight=True):
            add(word, "tfidf", "keyword", min(0.74, max(0.18, float(score) / 3.0)))

        return sorted(terms.values(), key=lambda item: (-item.weight, item.term))[: self.max_terms_per_bucket]

    def list_nodes(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = _int_between(limit, 50, 1, 500)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT
                    bucket_id,
                    term,
                    source,
                    kind,
                    weight,
                    updated_at
                FROM word_card_nodes
                """
            ).fetchall()
            grouped: dict[str, dict[str, Any]] = {}
            for row in rows:
                raw_term = str(row["term"] or "").strip()
                if self._is_overview_term_hidden(raw_term):
                    continue
                term = self._overview_canonical_term(raw_term)
                if self._is_overview_term_hidden(term):
                    continue
                item = grouped.setdefault(
                    term,
                    {
                        "term": term,
                        "kind": "keyword",
                        "bucket_ids": set(),
                        "weight": 0.0,
                        "max_weight": 0.0,
                        "sources": set(),
                        "aliases": set(),
                        "updated_at": str(row["updated_at"] or ""),
                    },
                )
                item["bucket_ids"].add(str(row["bucket_id"] or ""))
                weight = float(row["weight"] or 0.0)
                item["weight"] = float(item["weight"]) + weight
                item["max_weight"] = max(float(item["max_weight"]), weight)
                item["sources"].add(str(row["source"] or ""))
                if str(row["kind"] or "") == "subject" or str(row["source"] or "") in {"subject", "name"}:
                    item["kind"] = "subject"
                if raw_term != term:
                    item["aliases"].add(raw_term)
                item["updated_at"] = max(str(item["updated_at"] or ""), str(row["updated_at"] or ""))

            output = []
            for item in grouped.values():
                item["bucket_count"] = len(item.pop("bucket_ids"))
                item["sources"] = sorted(item["sources"])
                item["aliases"] = sorted(item["aliases"])
                item["overview_score"] = self._overview_node_score(item)
                output.append(item)
            output.sort(
                key=lambda item: (
                    -float(item.get("overview_score") or 0.0),
                    -float(item.get("max_weight") or 0.0),
                    -float(item.get("weight") or 0.0),
                    str(item.get("term") or ""),
                )
            )
            return output[:limit]
        finally:
            conn.close()

    def list_edges(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = _int_between(limit, 50, 1, 500)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT term_a, term_b, bucket_id, weight, updated_at
                FROM word_edges
                """
            ).fetchall()
            grouped: dict[tuple[str, str], dict[str, Any]] = {}
            for row in rows:
                left = str(row["term_a"] or "").strip()
                right = str(row["term_b"] or "").strip()
                if self._is_overview_term_hidden(left) or self._is_overview_term_hidden(right):
                    continue
                left = self._overview_canonical_term(left)
                right = self._overview_canonical_term(right)
                if self._is_overview_term_hidden(left) or self._is_overview_term_hidden(right):
                    continue
                if left == right:
                    continue
                term_a, term_b = sorted((left, right))
                item = grouped.setdefault(
                    (term_a, term_b),
                    {
                        "term_a": term_a,
                        "term_b": term_b,
                        "bucket_ids": set(),
                        "weight": 0.0,
                        "updated_at": str(row["updated_at"] or ""),
                    },
                )
                item["bucket_ids"].add(str(row["bucket_id"] or ""))
                item["weight"] = float(item["weight"]) + float(row["weight"] or 0.0)
                item["updated_at"] = max(str(item["updated_at"] or ""), str(row["updated_at"] or ""))

            output = []
            for item in grouped.values():
                item["bucket_count"] = len(item.pop("bucket_ids"))
                item["overview_score"] = self._overview_edge_score(item)
                output.append(item)
            output.sort(
                key=lambda item: (
                    -float(item.get("overview_score") or 0.0),
                    -float(item.get("weight") or 0.0),
                    -int(item.get("bucket_count") or 0),
                    str(item.get("term_a") or ""),
                    str(item.get("term_b") or ""),
                )
            )
            return output[:limit]
        finally:
            conn.close()

    def cards_for_term(self, term: str, limit: int = 20) -> list[dict[str, Any]]:
        cleaned = self._clean_term(term)
        if not cleaned:
            return []
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT bucket_id, term, source, kind, weight, updated_at
                FROM word_card_nodes
                WHERE term = ?
                ORDER BY weight DESC, bucket_id ASC
                LIMIT ?
                """,
                (cleaned, _int_between(limit, 20, 1, 200)),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def hint_buckets_for_terms(
        self,
        terms: list[str],
        *,
        neighbor_limit: int = 6,
        bucket_limit: int = 12,
    ) -> dict[str, Any]:
        """
        Return weak recall hints from direct word cards and one-hop co-occurrence.

        This is deliberately read-only and evidence-shaped: callers still decide
        whether a hinted bucket is allowed into visible recall.
        """
        if not self.enabled:
            return _empty_hint_payload()

        cleaned_terms = _unique_terms(self._clean_term(term) for term in terms)
        if not cleaned_terms:
            return _empty_hint_payload()

        neighbor_limit = _int_between(neighbor_limit, 6, 0, 40)
        bucket_limit = _int_between(bucket_limit, 12, 1, 100)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            neighbor_terms = [term for term in cleaned_terms if term not in self.weak_hint_terms]
            neighbor_scores = self._hint_neighbor_terms(conn, neighbor_terms, neighbor_limit)
            term_sources = {
                term: {
                    "kind": "direct",
                    "weight": self._hint_term_weight(term),
                    "sources": [term],
                }
                for term in cleaned_terms
            }
            for term, info in neighbor_scores.items():
                term_sources[term] = info

            card_terms = list(term_sources)
            if not card_terms:
                return _empty_hint_payload(cleaned_terms)
            placeholders = ",".join("?" for _ in card_terms)
            rows = conn.execute(
                f"""
                SELECT bucket_id, term, source, kind, weight, updated_at
                FROM word_card_nodes
                WHERE term IN ({placeholders})
                ORDER BY weight DESC, bucket_id ASC
                """,
                tuple(card_terms),
            ).fetchall()
        finally:
            conn.close()

        scores: dict[str, float] = {}
        evidence: dict[str, dict[str, Any]] = {}
        for row in rows:
            bucket_id = str(row["bucket_id"] or "").strip()
            term = str(row["term"] or "").strip()
            if not bucket_id or term not in term_sources:
                continue
            source_info = term_sources[term]
            try:
                card_weight = float(row["weight"] or 0.0)
            except (TypeError, ValueError):
                card_weight = 0.0
            contribution = max(0.0, min(1.0, card_weight)) * float(source_info.get("weight", 0.0))
            if contribution <= 0:
                continue
            scores[bucket_id] = min(1.0, scores.get(bucket_id, 0.0) + contribution)
            bucket_evidence = evidence.setdefault(
                bucket_id,
                {
                    "terms": [],
                    "direct_terms": [],
                    "neighbor_terms": [],
                },
            )
            row_payload = {
                "term": term,
                "kind": source_info.get("kind", ""),
                "score": round(contribution, 4),
                "source_terms": list(source_info.get("sources") or []),
                "card_source": str(row["source"] or ""),
                "weak_hint": term in self.weak_hint_terms,
            }
            bucket_evidence["terms"].append(row_payload)
            target_key = "direct_terms" if source_info.get("kind") == "direct" else "neighbor_terms"
            if term not in bucket_evidence[target_key]:
                bucket_evidence[target_key].append(term)

        ranked_ids = sorted(scores, key=lambda bucket_id: (-scores[bucket_id], bucket_id))[:bucket_limit]
        return {
            "terms": cleaned_terms,
            "neighbors": [
                {
                    "term": term,
                    "score": round(float(info.get("weight", 0.0)), 4),
                    "source_terms": list(info.get("sources") or []),
                }
                for term, info in neighbor_scores.items()
            ],
            "bucket_scores": {bucket_id: round(scores[bucket_id], 4) for bucket_id in ranked_ids},
            "evidence": {bucket_id: evidence[bucket_id] for bucket_id in ranked_ids},
        }

    def _hint_neighbor_terms(
        self,
        conn: sqlite3.Connection,
        source_terms: list[str],
        neighbor_limit: int,
    ) -> dict[str, dict[str, Any]]:
        if neighbor_limit <= 0:
            return {}
        neighbors: dict[str, dict[str, Any]] = {}
        source_set = set(source_terms)
        for term in source_terms:
            rows = conn.execute(
                """
                SELECT
                    CASE WHEN term_a = ? THEN term_b ELSE term_a END AS neighbor,
                    COUNT(*) AS bucket_count,
                    SUM(weight) AS weight
                FROM word_edges
                WHERE term_a = ? OR term_b = ?
                GROUP BY neighbor
                ORDER BY bucket_count DESC, weight DESC, neighbor ASC
                LIMIT ?
                """,
                (term, term, term, neighbor_limit),
            ).fetchall()
            for row in rows:
                neighbor = self._clean_term(row["neighbor"])
                if not neighbor or neighbor in source_set:
                    continue
                try:
                    bucket_count = max(1, int(row["bucket_count"] or 1))
                    avg_weight = float(row["weight"] or 0.0) / bucket_count
                except (TypeError, ValueError):
                    avg_weight = 0.0
                weight = min(0.55, max(0.05, avg_weight * 0.55))
                info = neighbors.setdefault(
                    neighbor,
                    {"kind": "neighbor", "weight": 0.0, "sources": []},
                )
                info["weight"] = max(float(info.get("weight", 0.0)), weight)
                if term not in info["sources"]:
                    info["sources"].append(term)

        ranked = sorted(
            neighbors.items(),
            key=lambda item: (-float(item[1].get("weight", 0.0)), item[0]),
        )[:neighbor_limit]
        return dict(ranked)

    def stats(self) -> dict[str, int]:
        conn = sqlite3.connect(self.db_path)
        try:
            nodes = conn.execute("SELECT COUNT(*) FROM word_nodes").fetchone()[0]
            cards = conn.execute("SELECT COUNT(*) FROM word_card_nodes").fetchone()[0]
            edge_rows = conn.execute("SELECT COUNT(*) FROM word_edges").fetchone()[0]
            return {"nodes": int(nodes), "card_nodes": int(cards), "edge_evidence": int(edge_rows)}
        finally:
            conn.close()

    def _write_bucket(self, conn: sqlite3.Connection, bucket: dict[str, Any]) -> None:
        bucket_id = str(bucket.get("id") or "").strip()
        if not bucket_id:
            return
        now = now_iso()
        terms = self.extract_bucket_terms(bucket)
        for term in terms:
            conn.execute(
                """
                INSERT OR REPLACE INTO word_card_nodes
                (bucket_id, term, source, kind, weight, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (bucket_id, term.term, term.source, term.kind, float(term.weight), now),
            )
        edge_terms = sorted(terms[: self.edge_top_k], key=lambda item: item.term)
        for left, right in itertools.combinations(edge_terms, 2):
            if left.term == right.term:
                continue
            term_a, term_b = sorted((left.term, right.term))
            conn.execute(
                """
                INSERT OR REPLACE INTO word_edges
                (term_a, term_b, bucket_id, weight, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (term_a, term_b, bucket_id, round((left.weight + right.weight) / 2.0, 4), now),
            )

    def _refresh_node_stats(self, conn: sqlite3.Connection) -> None:
        now = now_iso()
        conn.execute("DELETE FROM word_nodes")
        rows = conn.execute(
            """
            SELECT term, kind, COUNT(DISTINCT bucket_id) AS bucket_count, SUM(weight) AS weight
            FROM word_card_nodes
            GROUP BY term, kind
            """
        ).fetchall()
        for term, kind, bucket_count, weight in rows:
            conn.execute(
                """
                INSERT OR REPLACE INTO word_nodes
                (term, kind, bucket_count, weight, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (term, kind, int(bucket_count), float(weight or 0.0), now),
            )

    def _clean_term(self, value: Any) -> str:
        term = _normalize_term(value)
        if not term:
            return ""
        if term in self.stopwords or term in self.private_terms:
            return ""
        if any(term.startswith(prefix) for prefix in self.stopword_prefixes):
            return ""
        if len(term) < self.min_term_len or len(term) > 40:
            return ""
        if re.fullmatch(r"[a-z0-9_.:-]+", term) and len(term) < 3:
            return ""
        if re.fullmatch(r"[\d.:-]+", term):
            return ""
        return term

    def _hint_term_weight(self, term: str) -> float:
        if term in self.weak_hint_terms:
            return self.weak_hint_weight
        return 1.0

    def _title_concept_terms(self, title: str) -> list[str]:
        title = str(title or "").strip()
        if not title:
            return []
        terms: list[str] = []

        def add_term(value: Any) -> None:
            term = self._clean_term(value)
            if not term or term == title:
                return
            term = self._overview_canonical_term(term)
            if self._is_overview_term_hidden(term) or term in terms:
                return
            terms.append(term)

        normalized_title = _normalize_term(title)
        alias_candidates = set(self.overview_priority_terms) | set(self.overview_aliases) | {
            _normalize_term(value) for value in self.overview_aliases.values()
        }
        for candidate in sorted(alias_candidates, key=lambda item: (-len(item), item)):
            if not candidate:
                continue
            if candidate in normalized_title:
                add_term(candidate)

        for match in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", title):
            add_term(match)

        for word, _score in jieba.analyse.extract_tags(title, topK=6, withWeight=True):
            add_term(word)

        return terms[:6]

    def _is_overview_term_hidden(self, value: Any) -> bool:
        term = _normalize_term(value)
        if not term:
            return True
        if "日印象" in term or "relationship_weather" in term:
            return True
        if re.fullmatch(r"[a-f0-9]{12,40}", term):
            return True
        if re.fullmatch(r"[a-f0-9]{8}-[a-f0-9-]{27,}", term):
            return True
        if term in self.stopwords or term in self.private_terms or term in self.overview_stopwords:
            return True
        if any(term.startswith(prefix) for prefix in self.stopword_prefixes):
            return True
        if any(term.startswith(prefix) for prefix in self.overview_stopword_prefixes):
            return True
        return False

    def _overview_canonical_term(self, value: Any) -> str:
        term = _normalize_term(value)
        return self.overview_aliases.get(term, str(value or "").strip())

    def _overview_node_score(self, item: dict[str, Any]) -> float:
        try:
            max_weight = float(item.get("max_weight") or item.get("weight") or 0.0)
            bucket_count = max(1, int(item.get("bucket_count") or 1))
        except (TypeError, ValueError):
            return 0.0
        term = str(item.get("term") or "")
        sources = set(item.get("sources") or [])
        source_bonus = self._overview_source_bonus(sources)
        if _normalize_term(term) in self.overview_priority_terms:
            source_bonus = max(source_bonus, 1.25)
        specificity_bonus = self._overview_specificity_bonus(term, sources)
        coverage_factor = 1.0 / (bucket_count ** self._overview_coverage_exponent(term, sources))
        return round(max_weight * coverage_factor * specificity_bonus * source_bonus, 4)

    def _overview_edge_score(self, item: dict[str, Any]) -> float:
        try:
            weight = float(item.get("weight") or 0.0)
            bucket_count = max(1, int(item.get("bucket_count") or 1))
        except (TypeError, ValueError):
            return 0.0
        term_bonus = (
            self._overview_specificity_bonus(str(item.get("term_a") or ""), set())
            + self._overview_specificity_bonus(str(item.get("term_b") or ""), set())
        ) / 2.0
        return round(weight * term_bonus / (bucket_count ** 0.42), 4)

    @staticmethod
    def _overview_source_bonus(sources: set[str]) -> float:
        if "title_keyword" in sources:
            return 1.45
        if "keyword" in sources:
            return 1.25
        if "tfidf" in sources:
            return 1.0
        if {"subject", "name"} & sources:
            return 0.9
        if "tag" in sources:
            return 0.65
        if "domain" in sources:
            return 0.35
        return 0.8

    def _overview_specificity_bonus(self, term: str, sources: set[str]) -> float:
        normalized = _normalize_term(term)
        if not normalized:
            return 0.0
        score = 1.0
        exact_priority = normalized in self.overview_priority_terms
        is_title = bool({"subject", "name"} & sources)
        if exact_priority:
            score *= 1.8
        if re.search(r"[_-]", normalized) or re.search(r"[A-Za-z].*\d|\d.*[A-Za-z]", term):
            score *= 1.25
        if re.search(r"[A-Za-z]", term) and re.search(r"[\u4e00-\u9fff]", term):
            score *= 1.18
        if not is_title and any(
            marker in term for marker in ("机制", "暗房", "流星", "折角", "代码", "梦境", "显影", "外部", "验证")
        ):
            score *= 1.28
        chinese_chars = re.findall(r"[\u4e00-\u9fff]", term)
        if chinese_chars:
            if len(chinese_chars) <= 6:
                score *= 1.16
            elif len(chinese_chars) >= 10 and is_title:
                score *= 0.52
        if is_title and not exact_priority and any(
            marker in term
            for marker in (
                "上线",
                "行为约定",
                "身份配置",
                "外部验证",
                "接入",
                "配置",
                "调试",
                "计划",
                "待完成",
                "偏好",
                "调整",
                "问题",
                "恢复",
                "确认",
                "系统",
                "功能",
                "部署",
            )
        ):
            score *= 0.42
        elif is_title and not exact_priority and any(
            marker in term
            for marker in (
                "上线",
                "配置",
                "接入",
                "调试",
                "计划",
                "待完成",
                "偏好",
                "调整",
                "问题",
                "恢复",
                "确认",
                "系统",
            )
        ):
            score *= 0.68
        if normalized in {"mcp", "dashboard", "codex"}:
            score *= 0.78
        return round(score, 4)

    def _overview_coverage_exponent(self, term: str, sources: set[str]) -> float:
        normalized = _normalize_term(term)
        if normalized in self.overview_priority_terms or "title_keyword" in sources:
            return 0.24
        if re.search(r"[_-]", normalized) or re.search(r"[A-Za-z]", term):
            return 0.3
        return 0.42


def _bucket_text(bucket: dict[str, Any]) -> str:
    meta = bucket.get("metadata", {}) if isinstance(bucket.get("metadata"), dict) else {}
    parts = [
        str(meta.get("name") or ""),
        " ".join(_list_text(meta.get("tags"))),
        " ".join(_list_text(meta.get("domain"))),
        strip_wikilinks(strip_affect_anchor(str(bucket.get("content") or ""))),
    ]
    return " ".join(part for part in parts if part.strip())


def _list_text(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _normalize_term(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    text = text.strip("\"'`“”‘’[]【】()（）")
    return text.lower() if re.fullmatch(r"[A-Za-z0-9_.:/ -]+", text) else text


def _unique_terms(terms: Any) -> list[str]:
    output = []
    seen = set()
    for term in terms or []:
        cleaned = str(term or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        output.append(cleaned)
    return output


def _empty_hint_payload(terms: list[str] | None = None) -> dict[str, Any]:
    return {
        "terms": terms or [],
        "neighbors": [],
        "bucket_scores": {},
        "evidence": {},
    }


def _identity_stopwords(config: dict[str, Any]) -> list[str]:
    identity = config.get("identity", {}) if isinstance(config.get("identity", {}), dict) else {}
    values = [
        identity.get("ai_name"),
        identity.get("user_name"),
        identity.get("user_display_name"),
    ]
    values.extend(identity.get("user_aliases") or [])
    return [str(item).strip() for item in values if str(item).strip()]


def reflection_identity_terms(config: dict[str, Any]) -> list[str]:
    reflection = config.get("reflection", {}) if isinstance(config.get("reflection", {}), dict) else {}
    role_edges = reflection.get("identity_role_edges", {}) if isinstance(reflection.get("identity_role_edges", {}), dict) else {}
    if not role_edges.get("enabled"):
        return []

    terms: list[str] = []
    for section in ("detail", "context", "relationship", "shared"):
        terms.extend(_collect_config_terms(role_edges.get(section)))
    return terms


def _collect_config_terms(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        terms: list[str] = []
        for key, child in value.items():
            if str(key).strip():
                terms.append(str(key).strip())
            terms.extend(_collect_config_terms(child))
        return terms
    if isinstance(value, (list, tuple, set)):
        terms: list[str] = []
        for item in value:
            terms.extend(_collect_config_terms(item))
        return terms
    text = str(value).strip()
    return [text] if text else []


def _int_between(value: Any, default: int, lower: int, upper: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(lower, min(upper, number))


def _float_between(value: Any, default: float, lower: float, upper: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(lower, min(upper, number))


def dumps_debug(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
