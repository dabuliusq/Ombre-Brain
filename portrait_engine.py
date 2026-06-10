import json
import logging
import os
import re
from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

from identity import identity_names, render_identity_template
from utils import bucket_text_for_embedding, strip_wikilinks

logger = logging.getLogger("ombre_brain.portrait")


PORTRAIT_SCOPES = ("user", "persona", "relationship")
PATCH_KEYS = (
    "add_recent",
    "add_recent_activity",
    "move_to_staging",
    "rewrite_mid_term",
    "rewrite_stable",
    "stable_candidate",
    "profile_fact_candidate",
    "skip",
)


PORTRAIT_PROMPT_TEMPLATE = """你是 {ai_name}，正在维护你和 {user_display_name} 的换窗画像。
只根据证据写观察，不补常识，不把短期情绪当长期事实，也不要把关系气候误写成用户事实。

你会收到 previous_portrait 和 memory_materials。请输出纯 JSON：
{{
  "daily_summary": "今天发生了什么，最多80字",
  "add_recent": [
    {{
      "scope": "user|persona|relationship",
      "text": "一条短观察",
      "evidence": [{{"bucket_id": "证据桶id", "moment_id": ""}}],
      "confidence": 0.72
    }}
  ],
  "add_recent_activity": [
    {{
      "text": "{user_display_name}最近在做/推进的具体事，最多60字",
      "evidence": [{{"bucket_id": "证据桶id", "moment_id": ""}}],
      "confidence": 0.72
    }}
  ],
  "move_to_staging": [],
  "rewrite_mid_term": [
    {{
      "scope": "user|persona|relationship",
      "text": "最近几周的综合画像，只能从 staging_pool 或本次 move_to_staging 的证据得出",
      "evidence": [{{"bucket_id": "证据桶id"}}],
      "confidence": 0.72
    }}
  ],
  "rewrite_stable": [
    {{
      "scope": "user|persona|relationship",
      "text": "长期稳定画像。必须是一整段，可在 previous_portrait.stable 基础上维护",
      "evidence": [{{"bucket_id": "证据桶id"}}],
      "confidence": 0.82
    }}
  ],
  "stable_candidate": [],
  "profile_fact_candidate": [],
  "skip": []
}}

边界：
- user 只写 {user_display_name} 的证据化状态、偏好、边界；“最近在做什么”优先写 add_recent_activity。
- persona 写 {ai_name} 的自我理解和回复姿态。
- relationship 写这段关系的边界、里程碑和气候。
- add_recent_activity 只回答“{user_display_name}最近在做什么/推进什么/忙什么”，偏项目、生活事项、正在处理的问题；不要写纯情绪、关系天气或长期偏好。
- 不要滥用“{user_display_name}喜欢...”。只有证据明确表达稳定偏好、反复选择或清楚的喜欢时才写 user 偏好；关系天气、撒娇、确认、互动模式优先写 relationship。
- initial_run=true 时，add_recent 和 add_recent_activity 只放真正短期/当天或最近几天观察；高置信、能跨窗口携带的观察应放入 move_to_staging。每个 scope 尽量给 1-3 条 move_to_staging，除非证据不足。
- rewrite_mid_term 要把一个 scope 维护成一整段，不要输出多条近似碎片；它可综合 staging_pool 或本次 move_to_staging。
- rewrite_stable 要把一个 scope 的长期画像维护成一整段，在 previous_portrait.stable 基础上增删改；只有跨多日反复出现或已经由 mid_term/staging 支撑、未来换窗仍有用时才写。
- 输出要克制：daily_summary 最多60字，add_recent 最多4条，add_recent_activity 最多3条，move_to_staging 最多8条，rewrite_mid_term 每个 scope 最多1条，rewrite_stable 每个 scope 最多1条，每条 text 最多160字。
- profile_fact_candidate 只提候选，不确认、不写入长期 profile_fact。
- stable_candidate 只提候选；如果你有足够证据更新 stable portrait，优先输出 rewrite_stable。
- rewrite_mid_term 只能综合 staging_pool 里的观察，或本次明确 move_to_staging 的观察；不要直接把当天新材料写成 mid_term。
- rewrite_stable 不能只根据当天新材料；必须有 previous_portrait 或 staging/mid-term 证据支撑。
- memory_materials 含路径、tags、created 日期、关键 moment/reflection 片段，以及 source_excerpt 原文短摘；优先读证据原味，不要要求更长正文。
- 每条 add/rewrite/candidate 都必须带 evidence；没有证据就放 skip。
- 输出 JSON 对象，不要 markdown，不要解释。"""


class DailyPortraitMaintainer:
    """Maintains an evidence-bound portrait state outside memory buckets."""

    def __init__(self, config: dict):
        self.config = config
        self.identity = identity_names(config)
        cfg = config.get("portrait", {}) if isinstance(config.get("portrait", {}), dict) else {}
        reflection_cfg = config.get("reflection", {}) if isinstance(config.get("reflection", {}), dict) else {}
        persona_cfg = config.get("persona", {}) if isinstance(config.get("persona", {}), dict) else {}
        dehy_cfg = config.get("dehydration", {}) if isinstance(config.get("dehydration", {}), dict) else {}

        self.enabled = self._bool(cfg.get("enabled", True), True)
        self.auto_enabled = self._bool(cfg.get("auto_enabled", True), True)
        self.auto_initial_enabled = self._bool(cfg.get("auto_initial_enabled", False), False)
        self.daily_enabled = self._bool(cfg.get("daily_enabled", True), True)
        self.timezone_name = str(
            cfg.get("timezone")
            or reflection_cfg.get("timezone")
            or "Asia/Shanghai"
        )
        try:
            self.tz = ZoneInfo(self.timezone_name)
        except Exception:
            self.tz = ZoneInfo("Asia/Shanghai")
        self.daily_hour = int(cfg.get("daily_hour", reflection_cfg.get("daily_hour", 4)))
        self.check_interval_minutes = max(
            5,
            int(cfg.get("check_interval_minutes", reflection_cfg.get("check_interval_minutes", 60))),
        )
        self.material_limit = max(1, int(cfg.get("material_limit", 18)))
        self.first_run_material_limit = max(self.material_limit, int(cfg.get("first_run_material_limit", 160)))
        self.source_excerpt_chars = max(1, int(cfg.get("source_excerpt_chars", 900)))
        self.recent_continuity_days = max(1, int(cfg.get("recent_continuity_days", 3)))
        self.persona_events_limit = max(0, int(cfg.get("persona_events_limit", 24)))
        self.recent_buffer_max = max(1, int(cfg.get("recent_buffer_max", 24)))
        self.staging_pool_max = max(1, int(cfg.get("staging_pool_max", 24)))
        self.candidate_max = max(1, int(cfg.get("candidate_max", 40)))
        self.recent_timeline_max = max(self.recent_buffer_max, int(cfg.get("recent_timeline_max", 48)))
        self.base_url = (
            os.environ.get("OMBRE_PORTRAIT_BASE_URL", "")
            or cfg.get("base_url")
            or reflection_cfg.get("base_url")
            or persona_cfg.get("base_url")
            or dehy_cfg.get("base_url", "")
        )
        self.model = (
            os.environ.get("OMBRE_PORTRAIT_MODEL", "")
            or cfg.get("model")
            or reflection_cfg.get("model")
            or persona_cfg.get("model")
            or dehy_cfg.get("model", "deepseek-chat")
        )
        self.api_key = (
            os.environ.get("OMBRE_PORTRAIT_API_KEY", "")
            or cfg.get("api_key", "")
            or os.environ.get("OMBRE_REFLECTION_API_KEY", "")
            or reflection_cfg.get("api_key", "")
            or persona_cfg.get("api_key", "")
            or os.environ.get("OMBRE_PERSONA_API_KEY", "")
            or dehy_cfg.get("api_key", "")
        )
        self.thinking_mode = str(
            cfg.get("thinking_mode")
            or reflection_cfg.get("thinking_mode")
            or persona_cfg.get("thinking_mode")
            or ""
        ).strip()
        self.temperature = float(cfg.get("temperature", reflection_cfg.get("temperature", 0.1)))
        self.max_tokens = int(cfg.get("max_tokens", 1800))
        self.state_path = self._state_path(cfg.get("state_path", ""))
        self.client = None
        if self.enabled and self.api_key and self.base_url:
            self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, timeout=45.0)

    async def maintain_daily(
        self,
        bucket_mgr,
        persona_engine=None,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> dict:
        if not self.enabled:
            return {"status": "disabled", "reason": "portrait_disabled"}
        if not self.daily_enabled:
            return {"status": "skipped", "reason": "daily_disabled"}

        now_local = self._local_now(now)
        date_key = now_local.date().isoformat()
        state = self.load_state()
        if self._has_run_for_date(state, date_key) and not force:
            return {
                "status": "exists",
                "date": date_key,
                "state_path": self.state_path,
                "updated_at": state.get("updated_at", ""),
            }

        initial = not bool(state.get("runs"))
        if initial and not force and not self.auto_initial_enabled:
            return {
                "status": "skipped",
                "reason": "initial_requires_manual",
                "date": date_key,
                "state_path": self.state_path,
                "initial": True,
            }
        materials = await self._daily_materials(
            bucket_mgr,
            persona_engine,
            now_local,
            state,
            initial=initial,
        )
        if not materials["buckets"] and not materials["persona_events"] and not force:
            return {
                "status": "empty",
                "date": date_key,
                "state_path": self.state_path,
                "initial": initial,
            }

        raw_patch = await self._generate_patch(date_key, state, materials, initial=initial)
        normalized_patch, rejected = self._normalize_patch(raw_patch, materials)
        self._annotate_patch_source_dates(normalized_patch, materials)
        if initial:
            # Initial portrait generation scans broad history; its summary is not a real daily recap.
            normalized_patch["daily_summary"] = ""
            self._demote_initial_old_recent(normalized_patch, materials)
        handoff_summaries = self._build_handoff_recent_summaries(
            materials,
            normalized_patch,
            date_key,
        )
        if handoff_summaries:
            normalized_patch["handoff_recent_summaries"] = handoff_summaries
        recent_timeline = self._build_recent_timeline(materials, normalized_patch, date_key)
        if recent_timeline:
            normalized_patch["recent_timeline"] = recent_timeline
        next_state = self._apply_patch(state, normalized_patch, date_key)
        next_state["updated_at"] = self._now_utc()
        next_state.setdefault("runs", []).append(
            {
                "date": date_key,
                "created_at": next_state["updated_at"],
                "initial": initial,
                "material_count": len(materials["buckets"]),
                "persona_event_count": len(materials["persona_events"]),
                "patch_counts": {key: len(normalized_patch.get(key, [])) for key in PATCH_KEYS},
                "rejected_count": len(rejected),
                "model": self.model if self.client else "deterministic-fallback",
            }
        )
        next_state["runs"] = next_state["runs"][-90:]
        run_dates = [
            str(row.get("date") or "")
            for row in next_state.get("runs", [])
            if isinstance(row, dict) and str(row.get("date") or "")
        ]
        next_state["last_run_date"] = max(run_dates) if run_dates else date_key
        self.save_state(next_state)
        return {
            "status": "updated" if state.get("runs") else "initialized",
            "date": date_key,
            "state_path": self.state_path,
            "initial": initial,
            "materials": {
                "buckets": len(materials["buckets"]),
                "persona_events": len(materials["persona_events"]),
            },
            "patch_counts": {key: len(normalized_patch.get(key, [])) for key in PATCH_KEYS},
            "rejected": rejected[:8],
        }

    async def run_due(self, bucket_mgr, persona_engine=None) -> list[dict]:
        if not self.enabled or not self.auto_enabled:
            return []
        now_local = self._local_now()
        if not self.daily_enabled or now_local.hour < self.daily_hour:
            return []
        daily_date = (now_local - timedelta(days=1)).date()
        state = self.load_state()
        target_date = daily_date.isoformat()
        run_dates = [
            str(row.get("date") or "")
            for row in state.get("runs", [])
            if isinstance(row, dict) and str(row.get("date") or "")
        ]
        if any(date >= target_date for date in run_dates):
            return []
        daily_target = datetime.combine(daily_date, time.max, tzinfo=self.tz)
        return [
            await self.maintain_daily(
                bucket_mgr,
                persona_engine,
                force=False,
                now=daily_target,
            )
        ]

    def _has_run_for_date(self, state: dict, date_key: str) -> bool:
        for row in state.get("runs", []) or []:
            if isinstance(row, dict) and str(row.get("date") or "") == date_key:
                return True
        return str(state.get("last_run_date") or "") == date_key

    def load_state(self) -> dict:
        state = self._empty_state()
        if not os.path.exists(self.state_path):
            return state
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Portrait state load failed: %s", exc)
            return state
        if not isinstance(data, dict):
            return state
        state = self._merge_state(state, data)
        self._drop_initial_daily_summaries(state)
        return state

    def save_state(self, state: dict) -> None:
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp_path = self.state_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, self.state_path)

    def reset_state(self) -> dict:
        state = self._empty_state()
        state["updated_at"] = self._now_utc()
        self.save_state(state)
        return {
            "status": "reset",
            "state_path": self.state_path,
            "updated_at": state["updated_at"],
            "initial": True,
        }

    def delete_state_item(
        self,
        *,
        area: str,
        scope: str = "",
        layer: str = "",
        index: int | None = None,
        text: str = "",
    ) -> dict:
        state = self.load_state()
        area = str(area or "").strip()
        scope = str(scope or "").strip()
        layer = str(layer or "").strip()
        expected_text = str(text or "").strip()

        if area == "portrait":
            if scope not in PORTRAIT_SCOPES:
                return {"status": "invalid", "reason": "invalid_scope"}
            scope_state = state["portrait"][scope]
            if layer in {"stable", "mid_term"}:
                current = str(scope_state.get(layer) or "").strip()
                if not current:
                    return {"status": "not_found", "reason": "empty_layer"}
                if expected_text and self._norm(current) != self._norm(expected_text):
                    return {"status": "conflict", "reason": "text_mismatch"}
                if layer == "stable":
                    scope_state["stable"] = ""
                    scope_state["stable_evidence"] = []
                    scope_state["stable_source_dates"] = []
                    scope_state["stable_source_date"] = ""
                    scope_state["stable_updated_at"] = ""
                else:
                    scope_state["mid_term"] = ""
                    scope_state["mid_term_evidence"] = []
                    scope_state["mid_term_source_dates"] = []
                    scope_state["mid_term_source_date"] = ""
                    scope_state["mid_term_updated_at"] = ""
                state["updated_at"] = self._now_utc()
                self.save_state(state)
                return {"status": "deleted", "area": area, "scope": scope, "layer": layer}
            if layer not in {"recent_buffer", "staging_pool"}:
                return {"status": "invalid", "reason": "invalid_layer"}
            rows = scope_state.get(layer)
        elif area in {"recent_activities", "recent_timeline", "stable_candidates", "profile_fact_candidates", "skipped"}:
            rows = state.get(area)
            scope = ""
            layer = area
        else:
            return {"status": "invalid", "reason": "invalid_area"}

        if not isinstance(rows, list):
            return {"status": "invalid", "reason": "target_not_list"}
        found_index = self._find_row_index(rows, index=index, text=expected_text)
        if found_index is None:
            return {"status": "not_found", "reason": "row_not_found"}
        removed = rows.pop(found_index)
        state["updated_at"] = self._now_utc()
        self.save_state(state)
        return {
            "status": "deleted",
            "area": area,
            "scope": scope,
            "layer": layer,
            "index": found_index,
            "text": removed.get("text", "") if isinstance(removed, dict) else "",
        }

    def _find_row_index(self, rows: list, *, index: int | None, text: str) -> int | None:
        if index is not None and 0 <= index < len(rows):
            if not text:
                return index
            row = rows[index]
            if isinstance(row, dict) and self._norm(row.get("text", "")) == self._norm(text):
                return index
        if text:
            needle = self._norm(text)
            for idx, row in enumerate(rows):
                if isinstance(row, dict) and self._norm(row.get("text", "")) == needle:
                    return idx
        return None

    def build_handoff_sections(self, *, max_recent_items: int = 4) -> dict[str, str]:
        state = self.load_state()
        portrait = state.get("portrait", {}) if isinstance(state.get("portrait"), dict) else {}
        return {
            "user": self._format_scope_block(portrait.get("user", {})),
            "persona": self._format_scope_block(portrait.get("persona", {})),
            "relationship": self._format_scope_block(portrait.get("relationship", {})),
            "recent_continuity": self._format_recent_continuity(state, max_items=max_recent_items),
            "state_path": self.state_path,
            "updated_at": str(state.get("updated_at") or ""),
            "last_run_date": str(state.get("last_run_date") or ""),
        }

    async def _daily_materials(
        self,
        bucket_mgr,
        persona_engine,
        now_local: datetime,
        state: dict,
        *,
        initial: bool,
    ) -> dict:
        start, end = self._day_window(now_local)
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
        except Exception as exc:
            logger.warning("Portrait material bucket list failed: %s", exc)
            all_buckets = []

        buckets = []
        for bucket in all_buckets:
            if not self._is_material_bucket(bucket):
                continue
            meta = bucket.get("metadata", {}) if isinstance(bucket.get("metadata"), dict) else {}
            created = self._parse_iso(meta.get("created"))
            updated = self._parse_iso(meta.get("updated_at") or meta.get("last_active"))
            in_window = bool(
                (created and start <= created <= end)
                or (updated and start <= updated <= end)
            )
            if initial or in_window:
                buckets.append(bucket)

        buckets.sort(
            key=lambda item: str(
                item.get("metadata", {}).get("updated_at")
                or item.get("metadata", {}).get("created")
                or ""
            ),
            reverse=True,
        )
        limit = self.first_run_material_limit if initial else self.material_limit
        bucket_rows = [self._bucket_payload(bucket) for bucket in buckets[:limit]]
        return {
            "date": now_local.date().isoformat(),
            "initial": initial,
            "buckets": bucket_rows,
            "persona_events": self._persona_event_materials(persona_engine, start, end, initial=initial),
            "previous_portrait": self._portrait_snapshot(state),
        }

    async def _generate_patch(self, date_key: str, state: dict, materials: dict, *, initial: bool) -> dict:
        if self.client:
            try:
                return await self._api_patch(date_key, state, materials, initial=initial)
            except Exception as exc:
                logger.warning("Portrait LLM patch failed, using fallback: %s", exc)
        return self._fallback_patch(materials, initial=initial)

    async def _api_patch(self, date_key: str, state: dict, materials: dict, *, initial: bool) -> dict:
        payload = {
            "date": date_key,
            "initial_run": initial,
            "previous_portrait": materials.get("previous_portrait", {}),
            "memory_materials": {
                "buckets": materials.get("buckets", []),
                "persona_events": materials.get("persona_events", []),
            },
        }
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self._prompt()},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            **self._completion_options(max_tokens=self.max_tokens, temperature=self.temperature),
        )
        raw = response.choices[0].message.content if response.choices else "{}"
        return self._parse_json_object(raw or "{}")

    def _fallback_patch(self, materials: dict, *, initial: bool) -> dict:
        add_recent = []
        add_recent_activity = []
        move_to_staging = []
        recent_dates = self._recent_date_keys(str(materials.get("date") or ""))
        for bucket in materials.get("buckets", [])[:8]:
            scope = self._fallback_scope(bucket)
            text = self._fallback_text(bucket, scope)
            bucket_id = str(bucket.get("bucket_id") or "")
            if (
                bucket_id
                and len(add_recent_activity) < 3
                and (not initial or str(bucket.get("source_date") or "") in recent_dates)
            ):
                activity_text = self._fallback_activity_text(bucket)
                if activity_text and self._norm(activity_text) not in {
                    self._norm(item.get("text", "")) for item in add_recent_activity
                }:
                    add_recent_activity.append(
                        {
                            "text": activity_text,
                            "evidence": [{"bucket_id": bucket_id}],
                            "confidence": float(bucket.get("confidence") or 0.55),
                        }
                    )
            if not scope or not text or not bucket_id:
                continue
            row = {
                "scope": scope,
                "text": text,
                "evidence": [{"bucket_id": bucket_id}],
                "confidence": float(bucket.get("confidence") or 0.55),
            }
            if initial and self._fallback_initial_staging(bucket):
                move_to_staging.append(row)
            else:
                add_recent.append(row)
        daily_summary = "；".join(self._clip(item.get("name") or item.get("text"), 24) for item in materials.get("buckets", [])[:3] if item.get("name") or item.get("text"))
        return {
            "daily_summary": daily_summary,
            "add_recent": add_recent,
            "add_recent_activity": add_recent_activity,
            "move_to_staging": move_to_staging,
            "rewrite_mid_term": [],
            "stable_candidate": [],
            "profile_fact_candidate": [],
            "skip": [],
        }

    def _normalize_patch(self, patch: dict, materials: dict) -> tuple[dict, list[dict]]:
        if not isinstance(patch, dict):
            patch = {}
        normalized = {key: [] for key in PATCH_KEYS}
        rejected = []
        current_bucket_ids = {
            str(item.get("bucket_id") or "")
            for item in materials.get("buckets", [])
            if str(item.get("bucket_id") or "")
        }
        current_session_ids = {
            str(item.get("session_id") or "")
            for item in materials.get("persona_events", [])
            if str(item.get("session_id") or "")
        }
        portrait_bucket_ids, portrait_session_ids = self._portrait_evidence_sets(
            materials.get("previous_portrait", {})
        )
        staging_bucket_ids, staging_session_ids = self._portrait_evidence_sets(
            materials.get("previous_portrait", {}),
            staging_only=True,
        )
        known_bucket_ids = current_bucket_ids | portrait_bucket_ids
        known_session_ids = current_session_ids | portrait_session_ids

        for key in ("add_recent", "add_recent_activity", "move_to_staging"):
            raw_items = patch.get(key, [])
            if isinstance(raw_items, dict):
                raw_items = [raw_items]
            if not isinstance(raw_items, list):
                raw_items = []
            for item in raw_items:
                clean, reason = self._normalize_patch_item(
                    item,
                    key=key,
                    evidence_bucket_ids=known_bucket_ids,
                    evidence_session_ids=known_session_ids,
                )
                if clean:
                    normalized[key].append(clean)
                    if key == "move_to_staging":
                        self._add_evidence_to_sets(
                            clean.get("evidence", []),
                            staging_bucket_ids,
                            staging_session_ids,
                        )
                else:
                    rejected.append({"key": key, "reason": reason, "item": self._clip(str(item), 160)})

        for key, bucket_ids, session_ids, missing_reason in (
            ("rewrite_mid_term", staging_bucket_ids, staging_session_ids, "missing_staging_evidence"),
            ("rewrite_stable", known_bucket_ids, known_session_ids, "missing_valid_evidence"),
            ("stable_candidate", known_bucket_ids, known_session_ids, "missing_valid_evidence"),
            ("profile_fact_candidate", known_bucket_ids, known_session_ids, "missing_valid_evidence"),
            ("skip", set(), set(), "missing_valid_evidence"),
        ):
            raw_items = patch.get(key, [])
            if isinstance(raw_items, dict):
                raw_items = [raw_items]
            if not isinstance(raw_items, list):
                raw_items = []
            for item in raw_items:
                clean, reason = self._normalize_patch_item(
                    item,
                    key=key,
                    evidence_bucket_ids=bucket_ids,
                    evidence_session_ids=session_ids,
                    missing_reason=missing_reason,
                )
                if clean:
                    normalized[key].append(clean)
                else:
                    rejected.append({"key": key, "reason": reason, "item": self._clip(str(item), 160)})
        daily_summary = str(patch.get("daily_summary") or "").strip()
        if daily_summary:
            normalized["daily_summary"] = self._clip(daily_summary, 160)
        for key in ("rewrite_mid_term", "rewrite_stable"):
            by_scope = {}
            for item in normalized.get(key, []) or []:
                by_scope[item["scope"]] = item
            normalized[key] = [by_scope[scope] for scope in PORTRAIT_SCOPES if scope in by_scope]
        return normalized, rejected

    def _normalize_patch_item(
        self,
        item: Any,
        *,
        key: str,
        evidence_bucket_ids: set[str],
        evidence_session_ids: set[str],
        missing_reason: str = "missing_valid_evidence",
    ) -> tuple[dict | None, str]:
        if not isinstance(item, dict):
            return None, "not_object"
        scope = str(item.get("scope") or item.get("portrait") or item.get("section") or "").strip().lower()
        if key == "add_recent_activity":
            scope = "user"
        if scope not in PORTRAIT_SCOPES and key != "skip":
            return None, "invalid_scope"
        text = str(
            item.get("text")
            or item.get("summary")
            or item.get("fact")
            or item.get("reason")
            or ""
        ).strip()
        if not text:
            return None, "missing_text"
        evidence = self._normalize_evidence(
            item.get("evidence"),
            fallback_bucket_id=item.get("evidence_bucket_id") or item.get("bucket_id"),
            fallback_moment_id=item.get("evidence_moment_id") or item.get("moment_id"),
            fallback_session_id=item.get("session_id"),
        )
        if key != "skip":
            evidence = [
                row
                for row in evidence
                if row.get("bucket_id") in evidence_bucket_ids
                or row.get("session_id") in evidence_session_ids
            ]
            if not evidence:
                return None, missing_reason
        if key == "profile_fact_candidate" and not any(row.get("bucket_id") for row in evidence):
            return None, "profile_fact_needs_bucket_evidence"
        clean = {
            "scope": scope,
            "text": self._clip(text, 420),
            "evidence": evidence,
            "confidence": self._clamp(item.get("confidence"), 0.55),
        }
        if key == "profile_fact_candidate":
            clean["profile_kind"] = self._safe_key(item.get("profile_kind") or item.get("kind") or "other")
            clean["predicate"] = self._safe_key(item.get("predicate") or "")
            clean["object"] = self._clip(str(item.get("object") or ""), 120)
        return clean, ""

    def _demote_initial_old_recent(self, patch: dict, materials: dict) -> None:
        recent_bucket_ids, recent_session_ids = self._recent_material_evidence_ids(materials)
        kept = []
        for item in patch.get("add_recent", []) or []:
            evidence = item.get("evidence", []) if isinstance(item, dict) else []
            if self._evidence_intersects(evidence, recent_bucket_ids, recent_session_ids):
                kept.append(item)
            else:
                patch.setdefault("move_to_staging", []).append(item)
        patch["add_recent"] = kept
        kept_activities = []
        for item in patch.get("add_recent_activity", []) or []:
            evidence = item.get("evidence", []) if isinstance(item, dict) else []
            if self._evidence_intersects(evidence, recent_bucket_ids, recent_session_ids):
                kept_activities.append(item)
            else:
                patch.setdefault("skip", []).append({"text": item.get("text", ""), "scope": "user"})
        patch["add_recent_activity"] = kept_activities

    def _annotate_patch_source_dates(self, patch: dict, materials: dict) -> None:
        bucket_dates = {
            str(item.get("bucket_id") or ""): str(item.get("source_date") or "")
            for item in materials.get("buckets", []) or []
            if isinstance(item, dict) and str(item.get("bucket_id") or "") and str(item.get("source_date") or "")
        }
        session_dates = {
            str(item.get("session_id") or ""): str(item.get("source_date") or "")
            for item in materials.get("persona_events", []) or []
            if isinstance(item, dict) and str(item.get("session_id") or "") and str(item.get("source_date") or "")
        }
        previous_bucket_dates, previous_session_dates = self._portrait_evidence_date_maps(
            materials.get("previous_portrait", {})
        )
        for key in PATCH_KEYS:
            for item in patch.get(key, []) or []:
                if not isinstance(item, dict):
                    continue
                dates = set()
                for row in item.get("evidence", []) or []:
                    if not isinstance(row, dict):
                        continue
                    bucket_id = str(row.get("bucket_id") or "")
                    session_id = str(row.get("session_id") or "")
                    bucket_date = bucket_dates.get(bucket_id)
                    session_date = session_dates.get(session_id)
                    if bucket_date:
                        dates.add(bucket_date)
                    if session_date:
                        dates.add(session_date)
                    dates.update(previous_bucket_dates.get(bucket_id, []))
                    dates.update(previous_session_dates.get(session_id, []))
                if dates:
                    item["source_dates"] = sorted(dates, reverse=True)[:4]
                    item["source_date"] = item["source_dates"][0]

    def _portrait_evidence_date_maps(self, portrait: Any) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        bucket_dates: dict[str, set[str]] = {}
        session_dates: dict[str, set[str]] = {}
        if not isinstance(portrait, dict):
            return bucket_dates, session_dates

        def add_rows(rows: Any) -> None:
            for row in (rows if isinstance(rows, list) else []):
                if not isinstance(row, dict):
                    continue
                dates = set(self._merge_source_dates(row.get("source_dates", []), row.get("source_date", "")))
                if not dates:
                    continue
                for evidence in row.get("evidence", []) or []:
                    if not isinstance(evidence, dict):
                        continue
                    bucket_id = str(evidence.get("bucket_id") or "").strip()
                    session_id = str(evidence.get("session_id") or "").strip()
                    if bucket_id:
                        bucket_dates.setdefault(bucket_id, set()).update(dates)
                    if session_id:
                        session_dates.setdefault(session_id, set()).update(dates)

        for scope in PORTRAIT_SCOPES:
            scope_state = portrait.get(scope, {}) if isinstance(portrait.get(scope), dict) else {}
            add_rows(scope_state.get("recent_buffer", []))
            add_rows(scope_state.get("staging_pool", []))
            for prefix in ("mid_term", "stable"):
                dates = self._merge_source_dates(
                    scope_state.get(f"{prefix}_source_dates", []),
                    scope_state.get(f"{prefix}_source_date", ""),
                )
                if not dates:
                    continue
                for evidence in scope_state.get(f"{prefix}_evidence", []) or []:
                    if not isinstance(evidence, dict):
                        continue
                    bucket_id = str(evidence.get("bucket_id") or "").strip()
                    session_id = str(evidence.get("session_id") or "").strip()
                    if bucket_id:
                        bucket_dates.setdefault(bucket_id, set()).update(dates)
                    if session_id:
                        session_dates.setdefault(session_id, set()).update(dates)
        add_rows(portrait.get("recent_activities", []))
        return bucket_dates, session_dates

    def _recent_material_evidence_ids(self, materials: dict) -> tuple[set[str], set[str]]:
        date_key = str(materials.get("date") or "").strip()
        bucket_ids: set[str] = set()
        session_ids: set[str] = set()
        if not date_key:
            return bucket_ids, session_ids
        recent_dates = self._recent_date_keys(date_key)
        for item in materials.get("buckets", []) or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("source_date") or "") in recent_dates:
                bucket_id = str(item.get("bucket_id") or "").strip()
                if bucket_id:
                    bucket_ids.add(bucket_id)
        for item in materials.get("persona_events", []) or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("source_date") or "") in recent_dates:
                session_id = str(item.get("session_id") or "").strip()
                if session_id:
                    session_ids.add(session_id)
        return bucket_ids, session_ids

    def _recent_date_keys(self, date_key: str) -> set[str]:
        try:
            current = datetime.fromisoformat(date_key).date()
        except ValueError:
            return {date_key}
        return {
            (current - timedelta(days=offset)).isoformat()
            for offset in range(self.recent_continuity_days)
        }

    def _build_handoff_recent_summaries(self, materials: dict, patch: dict, date_key: str) -> dict[str, str]:
        recent_dates = self._recent_date_keys(date_key)
        by_date: dict[str, dict[str, list[str] | str]] = {}

        for bucket in materials.get("buckets", []) or []:
            if not isinstance(bucket, dict):
                continue
            source_date = str(bucket.get("source_date") or "").strip()
            if source_date not in recent_dates:
                continue
            tags = {str(tag).lower() for tag in bucket.get("tags", []) or []}
            if not ({"relationship_weather", "daily_impression"} & tags):
                continue
            text = self._handoff_weather_text(bucket)
            if not text:
                continue
            row = by_date.setdefault(source_date, {"weather": "", "excerpts": []})
            if not row.get("weather"):
                row["weather"] = text

        for event in materials.get("persona_events", []) or []:
            if not isinstance(event, dict):
                continue
            source_date = str(event.get("source_date") or "").strip()
            if source_date not in recent_dates:
                continue
            phrase = self._handoff_event_excerpt_phrase(event)
            if not phrase:
                continue
            excerpts = by_date.setdefault(source_date, {"weather": "", "excerpts": []})["excerpts"]
            if isinstance(excerpts, list) and phrase not in excerpts:
                excerpts.append(phrase)

        summaries: dict[str, str] = {}
        for summary_date in sorted(by_date.keys(), reverse=True):
            row = by_date[summary_date]
            excerpts = row.get("excerpts") if isinstance(row.get("excerpts"), list) else []
            weather = str(row.get("weather") or "").strip()
            parts = []
            if excerpts:
                parts.append("；".join(excerpts[:2]))
            if weather:
                parts.append(f"关系天气：{weather}")
            summary = "。".join(part.strip("。") for part in parts if part)
            if summary:
                summaries[summary_date] = self._clip(summary, 240)
        return summaries

    def _build_recent_timeline(self, materials: dict, patch: dict, date_key: str) -> list[dict]:
        recent_dates = self._recent_date_keys(date_key)
        buckets = {
            str(item.get("bucket_id") or ""): item
            for item in materials.get("buckets", []) or []
            if isinstance(item, dict) and str(item.get("bucket_id") or "")
        }
        sessions = {
            str(item.get("session_id") or ""): item
            for item in materials.get("persona_events", []) or []
            if isinstance(item, dict) and str(item.get("session_id") or "")
        }
        rows: list[dict] = []

        def append_item(item: dict, scope: str) -> None:
            if not isinstance(item, dict):
                return
            text = self._clip(item.get("text") or "", 180)
            if not text:
                return
            evidence = self._dedupe_evidence(item.get("evidence", []))
            if not evidence:
                return
            timestamp = self._timeline_timestamp_for_evidence(evidence, buckets, sessions)
            source_date = self._timeline_source_date(
                timestamp,
                item,
                evidence,
                buckets,
                sessions,
            )
            if source_date not in recent_dates:
                return
            row = {
                "scope": scope,
                "text": text,
                "evidence": evidence,
                "source_date": source_date,
                "source_dates": self._merge_source_dates([], item.get("source_dates", [])),
                "confidence": item.get("confidence", 0.55),
            }
            if timestamp:
                row["timestamp"] = timestamp.isoformat(timespec="minutes")
                row["time_label"] = timestamp.strftime("%Y-%m-%d %H:%M")
            rows.append(row)

        for item in patch.get("add_recent_activity", []) or []:
            append_item(item, "doing")
        for item in patch.get("add_recent", []) or []:
            append_item(item, str(item.get("scope") or "recent"))

        rows.sort(
            key=lambda row: (
                self._timeline_sort_value(row),
                self._recent_continuity_scope_priority(str(row.get("scope") or "")),
                self._norm(row.get("text", "")),
            ),
            reverse=True,
        )
        deduped = []
        seen = set()
        for row in rows:
            key = (self._norm(row.get("text", "")), str(row.get("scope") or ""))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        return deduped[: self.recent_timeline_max]

    def _timeline_timestamp_for_evidence(
        self,
        evidence: list[dict],
        buckets: dict[str, dict],
        sessions: dict[str, dict],
    ) -> datetime | None:
        candidates: list[datetime] = []
        for row in evidence:
            if not isinstance(row, dict):
                continue
            bucket_id = str(row.get("bucket_id") or "")
            session_id = str(row.get("session_id") or "")
            bucket = buckets.get(bucket_id)
            if bucket:
                for key in ("created", "updated_at"):
                    parsed = self._parse_iso(bucket.get(key))
                    if parsed:
                        candidates.append(parsed)
                        break
            session = sessions.get(session_id)
            if session:
                parsed = self._parse_iso(session.get("created_at"))
                if parsed:
                    candidates.append(parsed)
        return max(candidates) if candidates else None

    def _timeline_source_date(
        self,
        timestamp: datetime | None,
        item: dict,
        evidence: list[dict],
        buckets: dict[str, dict],
        sessions: dict[str, dict],
    ) -> str:
        if timestamp:
            return timestamp.date().isoformat()
        for value in self._merge_source_dates(item.get("source_dates", []), item.get("source_date", "")):
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                return value
        for row in evidence:
            bucket_id = str(row.get("bucket_id") or "") if isinstance(row, dict) else ""
            session_id = str(row.get("session_id") or "") if isinstance(row, dict) else ""
            bucket_date = str((buckets.get(bucket_id) or {}).get("source_date") or "")
            session_date = str((sessions.get(session_id) or {}).get("source_date") or "")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", bucket_date):
                return bucket_date
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", session_date):
                return session_date
        return ""

    def _handoff_weather_text(self, bucket: dict) -> str:
        text = str(bucket.get("text") or bucket.get("source_excerpt") or "").strip()
        text = self._clean_fallback_text(text)
        text = re.sub(r"^今天(?:的)?关系天气[：:]\s*", "", text)
        text = re.sub(r"^今天[：:]\s*", "", text)
        return self._clip(text, 180)

    def _handoff_event_excerpt_phrase(self, event: dict) -> str:
        user_excerpt = self._clean_handoff_excerpt(event.get("user_excerpt"))
        assistant_excerpt = self._clean_handoff_excerpt(event.get("assistant_excerpt"))
        user_name = str(self.identity.get("user_display_name") or "用户")
        ai_name = str(self.identity.get("ai_name") or "AI")
        parts = []
        if user_excerpt:
            parts.append(f"{user_name}说“{user_excerpt}”")
        if assistant_excerpt:
            parts.append(f"{ai_name}回“{assistant_excerpt}”")
        return self._clip("，".join(parts), 150)

    def _clean_handoff_excerpt(self, value: Any, *, max_chars: int = 72) -> str:
        text = strip_wikilinks(str(value or "")).strip()
        if not text:
            return ""
        text = re.sub(r"\s*<attachment\b[^>]*>.*?</attachment>\s*", " ", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"【当前时间】[^\n\r]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return self._clip(text, max_chars)

    def _bucket_source_date(self, meta: dict) -> str:
        explicit = str(meta.get("date") or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", explicit):
            return explicit
        for key in ("created", "updated_at", "last_active"):
            date_key = self._date_key_from_iso(meta.get(key))
            if date_key:
                return date_key
        return ""

    def _date_key_from_iso(self, value: Any) -> str:
        parsed = self._parse_iso(value)
        if not parsed:
            return ""
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self.tz)
        return parsed.astimezone(self.tz).date().isoformat()

    def _same_local_date(self, value: datetime | None, date_key: str) -> bool:
        if value is None:
            return False
        if value.tzinfo is None:
            value = value.replace(tzinfo=self.tz)
        return value.astimezone(self.tz).date().isoformat() == date_key

    def _evidence_intersects(self, evidence: Any, bucket_ids: set[str], session_ids: set[str]) -> bool:
        rows = evidence if isinstance(evidence, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("bucket_id") or "").strip() in bucket_ids:
                return True
            if str(row.get("session_id") or "").strip() in session_ids:
                return True
        return False

    def _apply_patch(self, state: dict, patch: dict, date_key: str) -> dict:
        state = self._merge_state(self._empty_state(), deepcopy(state))
        portrait = state["portrait"]
        if patch.get("daily_summary"):
            state.setdefault("daily_summaries", {})[date_key] = patch["daily_summary"]
            state["daily_summaries"] = dict(list(state["daily_summaries"].items())[-90:])
        if isinstance(patch.get("handoff_recent_summaries"), dict):
            summaries = state.setdefault("handoff_recent_summaries", {})
            for summary_date, summary_text in patch["handoff_recent_summaries"].items():
                summary_date = str(summary_date or "").strip()
                summary_text = self._clip(summary_text, 240)
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", summary_date) and summary_text:
                    summaries[summary_date] = summary_text
            state["handoff_recent_summaries"] = dict(sorted(summaries.items())[-90:])
        for item in patch.get("recent_timeline", []) or []:
            if isinstance(item, dict):
                self._upsert_recent_timeline_item(state["recent_timeline"], item, date_key)

        for item in patch.get("add_recent_activity", []):
            self._upsert_portrait_item(
                state["recent_activities"],
                item,
                date_key,
                max_items=self.recent_buffer_max,
            )
        for item in patch.get("add_recent", []):
            self._upsert_portrait_item(
                portrait[item["scope"]]["recent_buffer"],
                item,
                date_key,
                max_items=self.recent_buffer_max,
            )
        for item in patch.get("move_to_staging", []):
            recent = portrait[item["scope"]]["recent_buffer"]
            target_key = self._norm(item["text"])
            portrait[item["scope"]]["recent_buffer"] = [
                row for row in recent if self._norm(row.get("text", "")) != target_key
            ]
            self._upsert_portrait_item(
                portrait[item["scope"]]["staging_pool"],
                item,
                date_key,
                max_items=self.staging_pool_max,
            )
        for item in patch.get("rewrite_mid_term", []):
            scope_state = portrait[item["scope"]]
            scope_state["mid_term"] = item["text"]
            scope_state["mid_term_evidence"] = item["evidence"]
            source_dates = self._merge_source_dates([], item.get("source_dates", []))
            scope_state["mid_term_source_dates"] = source_dates
            scope_state["mid_term_source_date"] = source_dates[0] if source_dates else item.get("source_date", "")
            scope_state["mid_term_updated_at"] = self._now_utc()
        for item in patch.get("rewrite_stable", []):
            scope_state = portrait[item["scope"]]
            scope_state["stable"] = item["text"]
            scope_state["stable_evidence"] = item["evidence"]
            source_dates = self._merge_source_dates([], item.get("source_dates", []))
            scope_state["stable_source_dates"] = source_dates
            scope_state["stable_source_date"] = source_dates[0] if source_dates else item.get("source_date", "")
            scope_state["stable_updated_at"] = self._now_utc()
        for item in patch.get("stable_candidate", []):
            self._upsert_candidate(state["stable_candidates"], item, date_key)
        for item in patch.get("profile_fact_candidate", []):
            self._upsert_candidate(state["profile_fact_candidates"], item, date_key)
        for item in patch.get("skip", []):
            state.setdefault("skipped", []).append(
                {
                    "text": item["text"],
                    "scope": item.get("scope", ""),
                    "created_at": self._now_utc(),
                }
            )
        state["stable_candidates"] = state["stable_candidates"][-self.candidate_max:]
        state["profile_fact_candidates"] = state["profile_fact_candidates"][-self.candidate_max:]
        state["skipped"] = state.get("skipped", [])[-self.candidate_max:]
        return state

    def _upsert_portrait_item(self, rows: list[dict], item: dict, date_key: str, *, max_items: int) -> None:
        key = self._norm(item["text"])
        now = self._now_utc()
        for row in rows:
            if self._norm(row.get("text", "")) == key:
                row["text"] = item["text"]
                row["evidence"] = self._dedupe_evidence(row.get("evidence", []) + item.get("evidence", []))
                row["source_dates"] = self._merge_source_dates(row.get("source_dates", []), item.get("source_dates", []))
                row["source_date"] = row["source_dates"][0] if row["source_dates"] else row.get("source_date", "")
                row["confidence"] = max(float(row.get("confidence") or 0.0), float(item.get("confidence") or 0.0))
                row["last_seen_date"] = date_key
                row["updated_at"] = now
                row["count"] = int(row.get("count") or 1) + 1
                break
        else:
            rows.append(
                {
                    "text": item["text"],
                    "evidence": self._dedupe_evidence(item.get("evidence", [])),
                    "source_dates": self._merge_source_dates([], item.get("source_dates", [])),
                    "source_date": str(item.get("source_date") or ""),
                    "confidence": item.get("confidence", 0.55),
                    "first_seen_date": date_key,
                    "last_seen_date": date_key,
                    "created_at": now,
                    "updated_at": now,
                    "count": 1,
                }
            )
        rows.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
        del rows[max_items:]

    def _upsert_recent_timeline_item(self, rows: list[dict], item: dict, date_key: str) -> None:
        key = (self._norm(item.get("text", "")), str(item.get("scope") or ""))
        if not key[0]:
            return
        now = self._now_utc()
        for row in rows:
            row_key = (self._norm(row.get("text", "")), str(row.get("scope") or ""))
            if row_key != key:
                continue
            row["text"] = item["text"]
            row["evidence"] = self._dedupe_evidence(row.get("evidence", []) + item.get("evidence", []))
            row["source_dates"] = self._merge_source_dates(row.get("source_dates", []), item.get("source_dates", []))
            row["source_date"] = item.get("source_date") or row.get("source_date", "")
            row["confidence"] = max(float(row.get("confidence") or 0.0), float(item.get("confidence") or 0.0))
            incoming_time = self._parse_iso(item.get("timestamp"))
            current_time = self._parse_iso(row.get("timestamp"))
            if incoming_time and (not current_time or incoming_time >= current_time):
                row["timestamp"] = incoming_time.isoformat(timespec="minutes")
                row["time_label"] = incoming_time.strftime("%Y-%m-%d %H:%M")
            elif not row.get("time_label") and current_time:
                row["time_label"] = current_time.strftime("%Y-%m-%d %H:%M")
            row["last_seen_date"] = date_key
            row["updated_at"] = now
            row["count"] = int(row.get("count") or 1) + 1
            break
        else:
            timestamp = self._parse_iso(item.get("timestamp"))
            row = {
                "scope": str(item.get("scope") or "recent"),
                "text": item["text"],
                "evidence": self._dedupe_evidence(item.get("evidence", [])),
                "source_dates": self._merge_source_dates([], item.get("source_dates", [])),
                "source_date": str(item.get("source_date") or ""),
                "confidence": item.get("confidence", 0.55),
                "first_seen_date": date_key,
                "last_seen_date": date_key,
                "created_at": now,
                "updated_at": now,
                "count": 1,
            }
            if timestamp:
                row["timestamp"] = timestamp.isoformat(timespec="minutes")
                row["time_label"] = timestamp.strftime("%Y-%m-%d %H:%M")
            rows.append(row)
        rows.sort(
            key=lambda row: (
                self._timeline_sort_value(row),
                self._recent_continuity_scope_priority(str(row.get("scope") or "")),
                str(row.get("updated_at") or ""),
                self._norm(row.get("text", "")),
            ),
            reverse=True,
        )
        del rows[self.recent_timeline_max:]

    def _merge_source_dates(self, existing: Any, incoming: Any) -> list[str]:
        dates = {
            str(item or "").strip()
            for values in (existing, incoming)
            for item in (values if isinstance(values, list) else [values])
            if str(item or "").strip()
        }
        return sorted(dates, reverse=True)[:8]

    def _upsert_candidate(self, rows: list[dict], item: dict, date_key: str) -> None:
        key = self._norm(item["text"])
        now = self._now_utc()
        for row in rows:
            if self._norm(row.get("text", "")) == key and row.get("scope") == item.get("scope"):
                row["evidence"] = self._dedupe_evidence(row.get("evidence", []) + item.get("evidence", []))
                row["last_seen_date"] = date_key
                row["updated_at"] = now
                row["count"] = int(row.get("count") or 1) + 1
                return
        candidate = dict(item)
        candidate.update(
            {
                "first_seen_date": date_key,
                "last_seen_date": date_key,
                "created_at": now,
                "updated_at": now,
                "count": 1,
                "status": "candidate",
            }
        )
        rows.append(candidate)

    def _format_scope_block(self, scope_state: dict) -> str:
        if not isinstance(scope_state, dict):
            return ""
        lines = []
        if str(scope_state.get("stable") or "").strip():
            lines.append(f"Stable: {self._clip(scope_state['stable'], 260)}")
        if str(scope_state.get("mid_term") or "").strip():
            evidence = self._format_evidence(scope_state.get("mid_term_evidence", []))
            suffix = f" ({evidence})" if evidence else ""
            lines.append(f"Mid-term: {self._clip(scope_state['mid_term'], 260)}{suffix}")
        return "\n".join(line for line in lines if line.strip())

    def _format_recent_activity_block(self, state: dict, *, max_items: int) -> str:
        rows = state.get("recent_activities", []) if isinstance(state.get("recent_activities"), list) else []
        clean_rows = [row for row in rows if isinstance(row, dict) and str(row.get("text") or "").strip()]
        clean_rows.sort(
            key=lambda row: (
                self._row_source_date(row),
                str(row.get("updated_at") or row.get("created_at") or ""),
            ),
            reverse=True,
        )
        lines = []
        for row in clean_rows[: max(0, max_items)]:
            date_key = self._row_source_date(row)
            evidence = self._format_evidence(row.get("evidence", []))
            prefix = f"- {date_key}: " if date_key else "- "
            suffix = f" ({evidence})" if evidence else ""
            lines.append(f"{prefix}{self._clip(row.get('text', ''), 150)}{suffix}")
        return "\n".join(dict.fromkeys(line for line in lines if line.strip()))

    def _format_recent_continuity(self, state: dict, *, max_items: int) -> str:
        timeline = self._format_recent_timeline(state, max_items=max_items)
        if timeline:
            return timeline

        by_date: dict[str, list[tuple[str, dict]]] = {}
        handoff = (
            state.get("handoff_recent_summaries", {})
            if isinstance(state.get("handoff_recent_summaries"), dict)
            else {}
        )
        handoff_lines = []
        for date_key in sorted(handoff.keys(), reverse=True)[: self.recent_continuity_days]:
            summary = str(handoff.get(date_key) or "").strip()
            if summary:
                char_limit = 220 if not handoff_lines else 130
                handoff_lines.append(f"- {date_key}: {self._clip(summary, char_limit)}")
                if len(handoff_lines) >= max_items:
                    break
        if handoff_lines:
            return "\n".join(dict.fromkeys(handoff_lines))

        daily = state.get("daily_summaries", {}) if isinstance(state.get("daily_summaries"), dict) else {}
        for date_key, summary in list(daily.items())[-self.recent_continuity_days:]:
            if str(summary).strip():
                by_date.setdefault(str(date_key), []).append(("summary", {"text": str(summary)}))
        portrait = state.get("portrait", {}) if isinstance(state.get("portrait"), dict) else {}
        for scope in PORTRAIT_SCOPES:
            scope_state = portrait.get(scope, {}) if isinstance(portrait.get(scope), dict) else {}
            for row in scope_state.get("recent_buffer", []) or []:
                date_key = self._row_source_date(row)
                if not date_key:
                    continue
                by_date.setdefault(date_key, []).append((scope, row))
        lines = []
        emitted = 0
        date_keys = sorted(by_date.keys(), reverse=True)[: self.recent_continuity_days]
        reserved_old_days = max(0, len(date_keys) - 1)
        for day_index, date_key in enumerate(date_keys):
            rows = by_date[date_key]
            rows.sort(
                key=lambda item: (
                    self._recent_continuity_scope_priority(item[0]),
                    str(item[1].get("updated_at") or ""),
                ),
                reverse=True,
            )
            day_limit = max(1, max_items - reserved_old_days) if day_index == 0 else 1
            char_limit = 150 if day_index == 0 else 90
            for scope, row in rows[:day_limit]:
                if emitted >= max_items:
                    break
                evidence = self._format_evidence(row.get("evidence", []))
                prefix = "summary" if scope == "summary" else scope
                lines.append(
                    f"- {date_key} / {prefix}: {self._clip(row.get('text', ''), char_limit)}"
                    + (f" ({evidence})" if evidence and scope != "summary" else "")
                )
                emitted += 1
            if emitted >= max_items:
                break
        return "\n".join(dict.fromkeys(line for line in lines if line.strip()))

    @staticmethod
    def _recent_continuity_scope_priority(scope: str) -> int:
        return {
            "summary": 50,
            "doing": 45,
            "relationship": 40,
            "user": 30,
            "persona": 20,
        }.get(str(scope or ""), 10)

    def _format_recent_timeline(self, state: dict, *, max_items: int) -> str:
        rows = state.get("recent_timeline", []) if isinstance(state.get("recent_timeline"), list) else []
        clean_rows = [row for row in rows if isinstance(row, dict) and str(row.get("text") or "").strip()]
        if not clean_rows:
            return ""
        by_date: dict[str, list[dict]] = {}
        for row in clean_rows:
            date_key = self._timeline_date_key(row)
            if date_key:
                by_date.setdefault(date_key, []).append(row)
        lines = []
        emitted = 0
        date_keys = sorted(by_date.keys(), reverse=True)[: self.recent_continuity_days]
        reserved_old_days = max(0, len(date_keys) - 1)
        for day_index, date_key in enumerate(date_keys):
            day_rows = by_date[date_key]
            day_rows.sort(
                key=lambda row: (
                    self._timeline_sort_value(row),
                    self._recent_continuity_scope_priority(str(row.get("scope") or "")),
                    str(row.get("updated_at") or row.get("created_at") or ""),
                    self._norm(row.get("text", "")),
                ),
                reverse=True,
            )
            day_limit = max(1, max_items - reserved_old_days) if day_index == 0 else 1
            char_limit = 150 if day_index == 0 else 100
            for row in day_rows[:day_limit]:
                if emitted >= max_items:
                    break
                label = self._timeline_label(row)
                scope = self._recent_timeline_scope_label(str(row.get("scope") or "recent"))
                evidence = self._format_evidence(row.get("evidence", []))
                suffix = f" ({evidence})" if evidence else ""
                lines.append(f"- {label} / {scope}: {self._clip(row.get('text', ''), char_limit)}{suffix}")
                emitted += 1
            if emitted >= max_items:
                break
        return "\n".join(dict.fromkeys(line for line in lines if line.strip()))

    def _recent_timeline_scope_label(self, scope: str) -> str:
        return {
            "doing": "doing",
            "user": "user",
            "persona": "persona",
            "relationship": "relationship",
        }.get(str(scope or "").strip(), "recent")

    def _timeline_label(self, row: dict) -> str:
        parsed = self._parse_iso(row.get("timestamp"))
        if parsed:
            return parsed.strftime("%Y-%m-%d %H:%M")
        label = str(row.get("time_label") or "").strip()
        if label:
            return label
        return self._timeline_date_key(row)

    def _timeline_date_key(self, row: dict) -> str:
        parsed = self._parse_iso(row.get("timestamp"))
        if parsed:
            return parsed.date().isoformat()
        source_date = str(row.get("source_date") or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", source_date):
            return source_date
        return self._row_source_date(row)

    def _timeline_sort_value(self, row: dict) -> str:
        parsed = self._parse_iso(row.get("timestamp"))
        if parsed:
            return parsed.isoformat(timespec="minutes")
        date_key = self._timeline_date_key(row)
        if date_key:
            return f"{date_key}T00:00"
        return ""

    def _row_source_date(self, row: dict) -> str:
        for value in row.get("source_dates", []) or []:
            if str(value or "").strip():
                return str(value).strip()
        for key in ("source_date", "last_seen_date", "first_seen_date"):
            value = str(row.get(key) or "").strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                return value
        for key in ("updated_at", "created_at"):
            value = self._date_key_from_iso(row.get(key))
            if value:
                return value
        return ""

    def _bucket_payload(self, bucket: dict) -> dict:
        meta = bucket.get("metadata", {}) if isinstance(bucket.get("metadata"), dict) else {}
        key_sections = self._extract_key_sections(str(bucket.get("content") or ""))
        text = self._format_key_sections(key_sections)
        source_excerpt = self._clip(strip_wikilinks(bucket_text_for_embedding(bucket)), self.source_excerpt_chars)
        if not text:
            text = source_excerpt
        return {
            "bucket_id": str(bucket.get("id") or meta.get("id") or ""),
            "name": str(meta.get("name") or bucket.get("id") or ""),
            "path": str(bucket.get("path") or ""),
            "type": str(meta.get("type") or ""),
            "tags": [str(tag) for tag in meta.get("tags", []) or []],
            "domain": [str(item) for item in meta.get("domain", []) or []],
            "created": str(meta.get("created") or ""),
            "updated_at": str(meta.get("updated_at") or meta.get("last_active") or ""),
            "source_date": self._bucket_source_date(meta),
            "source": str(meta.get("source") or ""),
            "anchor": bool(meta.get("anchor")),
            "profile_kind": str(meta.get("profile_kind") or ""),
            "confidence": self._clamp(meta.get("confidence"), 0.55),
            "key_sections": key_sections,
            "text": self._clip(strip_wikilinks(text), 700),
            "source_excerpt": source_excerpt,
        }

    def _persona_event_materials(self, persona_engine, start: datetime, end: datetime, *, initial: bool) -> list[dict]:
        if not persona_engine or not hasattr(persona_engine, "get_dashboard_payload"):
            return []
        try:
            payload = persona_engine.get_dashboard_payload(events_limit=self.persona_events_limit)
        except Exception as exc:
            logger.warning("Portrait persona event lookup failed: %s", exc)
            return []
        rows = []
        for event in payload.get("events", []) or []:
            created = self._parse_iso(event.get("created_at"))
            if not initial and created and not (start <= created <= end):
                continue
            if not initial and not created:
                continue
            rows.append(
                {
                    "event_id": event.get("id"),
                    "session_id": str(event.get("session_id") or ""),
                    "created_at": str(event.get("created_at") or ""),
                    "source_date": self._date_key_from_iso(event.get("created_at")),
                    "event_type": str(event.get("event_type") or ""),
                    "perceived_intent": self._clip(event.get("perceived_intent") or "", 120),
                    "inner_thought": self._clip(event.get("inner_thought") or "", 80),
                    "user_excerpt": self._clip(event.get("user_excerpt") or "", 240),
                    "assistant_excerpt": self._clip(event.get("assistant_excerpt") or "", 240),
                    "reply_guidance": self._clip(event.get("reply_guidance") or "", 160),
                    "relationship_event": bool(event.get("relationship_event")),
                    "confidence": self._clamp(event.get("confidence"), 0.55),
                }
            )
            if len(rows) >= self.persona_events_limit:
                break
        return rows

    def _is_material_bucket(self, bucket: dict) -> bool:
        if not isinstance(bucket, dict):
            return False
        meta = bucket.get("metadata", {}) if isinstance(bucket.get("metadata"), dict) else {}
        if meta.get("active") is False or meta.get("deprecated"):
            return False
        if meta.get("pinned") or meta.get("protected"):
            return False
        if meta.get("type") == "archived":
            return False
        return True

    def _fallback_scope(self, bucket_payload: dict) -> str:
        tags = {str(tag).lower() for tag in bucket_payload.get("tags", []) or []}
        domains = {str(item).lower() for item in bucket_payload.get("domain", []) or []}
        if "profile_fact" in tags or bucket_payload.get("profile_kind"):
            return "user"
        if {"relationship_weather", "daily_impression", "weekly_impression"} & tags:
            return "relationship"
        if bucket_payload.get("type") == "feel" or bucket_payload.get("source") == "reflection":
            return "persona"
        if bucket_payload.get("anchor") or "恋爱" in domains or "relationship_event" in tags:
            return "relationship"
        return ""

    def _fallback_initial_staging(self, bucket_payload: dict) -> bool:
        tags = {str(tag).lower() for tag in bucket_payload.get("tags", []) or []}
        if tags & {"relationship_weather", "daily_impression", "weekly_impression"}:
            return False
        name = str(bucket_payload.get("name") or "")
        if re.search(r"\d{4}-\d{2}-\d{2}\s*(日印象|周印象)", name):
            return False
        return True

    def _fallback_text(self, bucket_payload: dict, scope: str) -> str:
        sections = bucket_payload.get("key_sections", []) if isinstance(bucket_payload, dict) else []

        def first_section(*names: str) -> str:
            wanted = {name.lower() for name in names}
            for section in sections:
                if not isinstance(section, dict):
                    continue
                if str(section.get("heading") or "").strip().lower() in wanted:
                    text = str(section.get("text") or "").strip()
                    if text:
                        return text
            return ""

        if scope == "persona":
            text = first_section("reflection", "assistant_reflection", "moment", "fact")
        elif scope == "user":
            text = first_section("fact", "moment")
        else:
            text = first_section("moment", "fact", "reflection", "assistant_reflection")
        if not text:
            text = str(bucket_payload.get("source_excerpt") or bucket_payload.get("text") or "")
        return self._clean_fallback_text(text)

    def _fallback_activity_text(self, bucket_payload: dict) -> str:
        tags = {str(tag).lower() for tag in bucket_payload.get("tags", []) or []}
        domains = {str(item).lower() for item in bucket_payload.get("domain", []) or []}
        text = self._clean_fallback_text(
            str(bucket_payload.get("source_excerpt") or bucket_payload.get("text") or "")
        )
        if not text:
            return ""
        project_like = bool(
            tags & {"project_event", "work_event", "task_event"}
            or domains & {"记忆系统", "代码", "工作", "项目", "开发"}
        )
        activity_like = bool(
            re.search(
                r"(小雨|她|我).{0,12}(最近在|这几天在|这两天在|正在|继续|开始|准备|推进|调整|修改|修|部署|测试|写|搭|研究|排查|调试|做)",
                text,
            )
            or re.search(r"(最近在|这几天在|这两天在|正在).{0,16}(推进|调整|修改|修|部署|测试|写|搭|研究|排查|调试|做)", text)
        )
        if project_like and not activity_like:
            activity_like = bool(re.search(r"(推进|调整|修改|修|部署|测试|写|搭|研究|排查|调试|做|实现|开发|准备|加)", text))
        if not activity_like:
            return ""
        if re.search(r"(关系天气|撒娇|亲密|喜欢被|确认我在)", text) and not project_like:
            return ""
        return self._clip(text, 120)

    def _clean_fallback_text(self, text: str) -> str:
        text = strip_wikilinks(str(text or ""))
        text = re.sub(r"^Title:\s*.*?\bContent:\s*", "", text, flags=re.DOTALL)
        text = re.split(r"\s*###\s+affect_anchor\b", text, maxsplit=1, flags=re.IGNORECASE)[0]
        text = re.sub(r"###\s+[\w\u4e00-\u9fff_ -]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip(" ：:;；。")
        return self._clip(text, 140)

    def _portrait_snapshot(self, state: dict) -> dict:
        portrait = state.get("portrait", {}) if isinstance(state.get("portrait"), dict) else {}
        snapshot = {
            scope: {
                "recent_buffer": (portrait.get(scope, {}) or {}).get("recent_buffer", [])[:8],
                "staging_pool": (portrait.get(scope, {}) or {}).get("staging_pool", [])[:8],
                "mid_term": (portrait.get(scope, {}) or {}).get("mid_term", ""),
                "mid_term_evidence": (portrait.get(scope, {}) or {}).get("mid_term_evidence", [])[:8],
                "mid_term_source_dates": (portrait.get(scope, {}) or {}).get("mid_term_source_dates", [])[:8],
                "stable": (portrait.get(scope, {}) or {}).get("stable", ""),
                "stable_evidence": (portrait.get(scope, {}) or {}).get("stable_evidence", [])[:8],
                "stable_source_dates": (portrait.get(scope, {}) or {}).get("stable_source_dates", [])[:8],
            }
            for scope in PORTRAIT_SCOPES
        }
        snapshot["recent_activities"] = (
            state.get("recent_activities", []) if isinstance(state.get("recent_activities"), list) else []
        )[:8]
        return snapshot

    def _portrait_evidence_sets(self, portrait: Any, *, staging_only: bool = False) -> tuple[set[str], set[str]]:
        bucket_ids: set[str] = set()
        session_ids: set[str] = set()
        if not isinstance(portrait, dict):
            return bucket_ids, session_ids
        for scope in PORTRAIT_SCOPES:
            scope_state = portrait.get(scope, {}) if isinstance(portrait.get(scope), dict) else {}
            rows = []
            if staging_only:
                rows.extend(scope_state.get("staging_pool", []) or [])
            else:
                rows.extend(scope_state.get("recent_buffer", []) or [])
                rows.extend(scope_state.get("staging_pool", []) or [])
                rows.append({"evidence": scope_state.get("mid_term_evidence", []) or []})
                rows.append({"evidence": scope_state.get("stable_evidence", []) or []})
            for row in rows:
                if not isinstance(row, dict):
                    continue
                self._add_evidence_to_sets(row.get("evidence", []), bucket_ids, session_ids)
        if not staging_only:
            for row in portrait.get("recent_activities", []) or []:
                if isinstance(row, dict):
                    self._add_evidence_to_sets(row.get("evidence", []), bucket_ids, session_ids)
        return bucket_ids, session_ids

    def _add_evidence_to_sets(
        self,
        evidence: Any,
        bucket_ids: set[str],
        session_ids: set[str],
    ) -> None:
        rows = evidence if isinstance(evidence, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            bucket_id = str(row.get("bucket_id") or "").strip()
            session_id = str(row.get("session_id") or "").strip()
            if bucket_id:
                bucket_ids.add(bucket_id)
            if session_id:
                session_ids.add(session_id)

    def _extract_key_sections(self, content: str) -> list[dict]:
        wanted = {"moment", "reflection", "assistant_reflection", "fact"}
        sections = []
        current_title = ""
        current_lines: list[str] = []

        def flush() -> None:
            nonlocal current_title, current_lines
            if current_title in wanted:
                text = "\n".join(current_lines).strip()
                if text:
                    sections.append(
                        {
                            "heading": current_title,
                            "text": self._clip(text, 360 if current_title == "moment" else 220),
                        }
                    )
            current_title = ""
            current_lines = []

        for line in str(content or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            match = re.match(r"^###\s+(.+?)\s*$", line.strip())
            if match:
                flush()
                current_title = match.group(1).strip().lower()
                current_lines = []
                continue
            if current_title:
                current_lines.append(line)
        flush()
        return sections[:5]

    def _format_key_sections(self, sections: list[dict]) -> str:
        parts = []
        for section in sections:
            if not isinstance(section, dict):
                continue
            heading = str(section.get("heading") or "").strip()
            text = str(section.get("text") or "").strip()
            if heading and text:
                parts.append(f"### {heading}\n{text}")
        return "\n\n".join(parts)

    def _prompt(self) -> str:
        return render_identity_template(PORTRAIT_PROMPT_TEMPLATE, self.identity)

    def _completion_options(self, *, max_tokens: int, temperature: float) -> dict[str, Any]:
        options: dict[str, Any] = {"max_tokens": max_tokens, "temperature": temperature}
        if self.thinking_mode:
            options["extra_body"] = {"thinking": {"type": self.thinking_mode}}
        return options

    def _parse_json_object(self, raw: str) -> dict:
        text = str(raw or "").strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            text = match.group(0)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Portrait JSON parse failed: %s", str(raw)[:200])
            raise ValueError("portrait_json_parse_failed")
        if not isinstance(parsed, dict):
            raise ValueError("portrait_json_not_object")
        return parsed

    def _normalize_evidence(
        self,
        value: Any,
        *,
        fallback_bucket_id: Any = "",
        fallback_moment_id: Any = "",
        fallback_session_id: Any = "",
    ) -> list[dict]:
        rows = []
        if isinstance(value, dict):
            value = [value]
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    rows.append({"bucket_id": item.strip()})
                elif isinstance(item, dict):
                    row = {
                        "bucket_id": str(item.get("bucket_id") or item.get("id") or "").strip(),
                        "moment_id": str(item.get("moment_id") or "").strip(),
                        "session_id": str(item.get("session_id") or "").strip(),
                    }
                    rows.append({k: v for k, v in row.items() if v})
        if not rows and (fallback_bucket_id or fallback_session_id):
            row = {
                "bucket_id": str(fallback_bucket_id or "").strip(),
                "moment_id": str(fallback_moment_id or "").strip(),
                "session_id": str(fallback_session_id or "").strip(),
            }
            rows.append({k: v for k, v in row.items() if v})
        return self._dedupe_evidence(rows)

    def _dedupe_evidence(self, rows: list[dict]) -> list[dict]:
        result = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            clean = {k: str(v).strip() for k, v in row.items() if str(v or "").strip()}
            if not clean:
                continue
            key = tuple(sorted(clean.items()))
            if key in seen:
                continue
            seen.add(key)
            result.append(clean)
        return result[:8]

    def _format_evidence(self, evidence: Any) -> str:
        rows = evidence if isinstance(evidence, list) else []
        labels = []
        for row in rows[:3]:
            if not isinstance(row, dict):
                continue
            if row.get("bucket_id"):
                label = f"bucket_id:{row['bucket_id']}"
                if row.get("moment_id"):
                    label += f"/moment_id:{row['moment_id']}"
                labels.append(label)
            elif row.get("session_id"):
                labels.append(f"session_id:{row['session_id']}")
        return ", ".join(labels)

    def _state_path(self, configured: Any) -> str:
        configured_text = str(configured or "").strip()
        if configured_text:
            return os.path.abspath(configured_text)
        state_dir = self.config.get("state_dir") or os.path.join(
            os.path.dirname(os.path.abspath(self.config.get("buckets_dir", "buckets"))),
            "state",
        )
        return os.path.join(state_dir, "portrait_state.json")

    def _empty_state(self) -> dict:
        return {
            "version": "portrait-state-v1",
            "updated_at": "",
            "last_run_date": "",
            "portrait": {
                scope: {
                    "recent_buffer": [],
                    "staging_pool": [],
                    "mid_term": "",
                    "mid_term_evidence": [],
                    "mid_term_source_dates": [],
                    "mid_term_source_date": "",
                    "mid_term_updated_at": "",
                    "stable": "",
                    "stable_evidence": [],
                    "stable_source_dates": [],
                    "stable_source_date": "",
                    "stable_updated_at": "",
                }
                for scope in PORTRAIT_SCOPES
            },
            "daily_summaries": {},
            "handoff_recent_summaries": {},
            "recent_activities": [],
            "recent_timeline": [],
            "stable_candidates": [],
            "profile_fact_candidates": [],
            "skipped": [],
            "runs": [],
        }

    def _merge_state(self, base: dict, data: dict) -> dict:
        for key, value in data.items():
            if key == "portrait" and isinstance(value, dict):
                for scope in PORTRAIT_SCOPES:
                    if isinstance(value.get(scope), dict):
                        base["portrait"][scope].update(value[scope])
            elif key in {"daily_summaries", "handoff_recent_summaries"} and isinstance(value, dict):
                base[key] = value
            elif key in {"recent_activities", "recent_timeline", "stable_candidates", "profile_fact_candidates", "skipped", "runs"} and isinstance(value, list):
                base[key] = value
            elif key in {"version", "updated_at", "last_run_date"}:
                base[key] = str(value or "")
        return base

    def _drop_initial_daily_summaries(self, state: dict) -> None:
        daily = state.get("daily_summaries")
        runs = state.get("runs")
        if not isinstance(daily, dict) or not isinstance(runs, list):
            return
        initial_dates = {
            str(row.get("date") or "")
            for row in runs
            if isinstance(row, dict) and row.get("initial") and str(row.get("date") or "")
        }
        non_initial_dates = {
            str(row.get("date") or "")
            for row in runs
            if isinstance(row, dict) and not row.get("initial") and str(row.get("date") or "")
        }
        for date_key in initial_dates - non_initial_dates:
            daily.pop(date_key, None)

    def _day_window(self, now_local: datetime) -> tuple[datetime, datetime]:
        day = now_local.date()
        return (
            datetime.combine(day, time.min, tzinfo=self.tz),
            datetime.combine(day, time.max, tzinfo=self.tz),
        )

    def _local_now(self, value: datetime | None = None) -> datetime:
        if value is None:
            return datetime.now(self.tz)
        if value.tzinfo is None:
            value = value.replace(tzinfo=self.tz)
        return value.astimezone(self.tz)

    def _parse_iso(self, value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(self.tz)

    def _now_utc(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _safe_key(self, value: Any) -> str:
        text = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "_", str(value or "").strip())
        return text[:80].strip("_") or "other"

    def _norm(self, value: str) -> str:
        return re.sub(r"\s+", "", str(value or "").lower())

    def _clip(self, value: Any, max_chars: int) -> str:
        text = " ".join(strip_wikilinks(str(value or "")).split())
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + "..."

    def _clamp(self, value: Any, default: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = default
        return max(0.0, min(1.0, number))

    def _bool(self, value: Any, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}
