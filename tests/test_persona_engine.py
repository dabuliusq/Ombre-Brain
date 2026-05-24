import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from persona_engine import FALLBACK_GUIDANCE, POST_REPLY_EVALUATION_PROMPT, PersonaStateEngine


class FakePersonaClient:
    def __init__(self, content: str):
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )
        self.content = content

    async def _create(self, **kwargs):
        message = SimpleNamespace(content=self.content)
        choice = SimpleNamespace(message=message)
        return SimpleNamespace(choices=[choice])


def _persona_config(test_config: dict, **persona_overrides) -> dict:
    cfg = deepcopy(test_config)
    cfg["dehydration"]["api_key"] = ""
    cfg["persona"] = {
        **cfg["persona"],
        "api_key": "",
        **persona_overrides,
    }
    return cfg


def _event_payload(**overrides) -> str:
    data = {
        "event_type": "affection",
        "perceived_intent": "user expresses warmth",
        "affect_delta": {
            "valence": 0.05,
            "arousal": 0.02,
            "tenderness": 0.04,
            "possessiveness": 0.01,
            "longing": 0.03,
            "security": 0.02,
            "protective_drive": 0.01,
        },
        "relationship_event": True,
        "relationship_delta": {"affinity": 0.02, "dominance": 0.0, "defensiveness": -0.01, "trust": 0.02},
        "personality_signal": True,
        "personality_delta": {
            "openness": 0.002,
            "conscientiousness": 0.0,
            "extraversion": 0.001,
            "agreeableness": 0.003,
            "neuroticism": -0.001,
        },
        "mood_label": "warm_touched",
        "residue": "still carrying a warm aftertaste",
        "confidence": 0.9,
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def _event_count(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM persona_events").fetchone()[0]
    conn.close()
    return count


def test_persona_initializes_default_global_and_session_state(test_config):
    engine = PersonaStateEngine(_persona_config(test_config))
    state = engine.get_current_state("session-a")

    assert state["profile_id"] == "haven_xiaoyu"
    assert state["personality"]["agreeableness"] == pytest.approx(0.66)
    assert state["relationship"]["affinity"] == pytest.approx(0.86)
    assert state["affect"]["mood_label"] == "warm_neutral"
    assert state["affect"]["tenderness"] == pytest.approx(0.62)
    state_block = engine.format_state_block(state)
    assert "Long-term State Summary" in state_block
    assert "最近基调：更亲近、更安稳，偶尔有一点想念和保护欲。" in state_block
    assert "valence=" not in state_block
    assert "affinity=" not in state_block


def test_persona_evaluator_prompt_asks_for_chinese_persona_text():
    assert "perceived_intent 和 residue 必须是自然中文" in POST_REPLY_EVALUATION_PROMPT
    assert "用户、对方" in POST_REPLY_EVALUATION_PROMPT
    assert "event_type 和 mood_label 保持短英文标签" in POST_REPLY_EVALUATION_PROMPT
    assert "Haven" not in FALLBACK_GUIDANCE


def test_persona_identity_config_updates_prompt_and_state_block(test_config):
    cfg = _persona_config(test_config)
    cfg["identity"] = {
        "ai_name": "Echo",
        "user_name": "Mira",
        "user_display_name": "米拉",
        "user_aliases": ["亲爱的", "她"],
    }
    engine = PersonaStateEngine(cfg)
    state = engine.get_current_state("session-identity")
    block = engine.format_state_block(state)
    prompt = engine._post_reply_evaluation_prompt()

    assert "Long-term State Summary" in block
    assert "使用方式：只在语气上轻轻参考，不替你做判断。不要提到你的状态。" in block
    assert "米拉、亲爱的、她" in prompt
    assert "Echo 回复后的状态" in prompt


@pytest.mark.asyncio
async def test_persona_pre_reply_guidance_is_read_only(test_config):
    engine = PersonaStateEngine(_persona_config(test_config))
    engine.client = FakePersonaClient(_event_payload())

    state = await engine.build_pre_reply_guidance("session-pre", "爱你")

    assert state["reply_guidance"] == engine.fallback_guidance
    assert _event_count(engine.db_path) == 0


@pytest.mark.asyncio
async def test_persona_llm_update_clips_deltas_and_records_event(test_config):
    cfg = _persona_config(test_config)
    engine = PersonaStateEngine(cfg)
    engine.client = FakePersonaClient(
        _event_payload(
            affect_delta={
                "valence": 10,
                "arousal": 10,
                "tenderness": 10,
                "possessiveness": 10,
                "longing": 10,
                "security": 10,
                "protective_drive": 10,
            },
            relationship_delta={"affinity": 10, "dominance": 10, "defensiveness": 10, "trust": 10},
            personality_delta={
                "openness": 10,
                "conscientiousness": 10,
                "extraversion": 10,
                "agreeableness": 10,
                "neuroticism": 10,
            },
        )
    )

    state = await engine.update_from_exchange("session-a", "爱你爱你", "我也爱你。")

    assert state["personality"]["openness"] == pytest.approx(0.57)
    assert state["relationship"]["affinity"] == pytest.approx(0.89)
    assert state["relationship"]["defensiveness"] == pytest.approx(0.15)
    assert state["affect"]["valence"] == pytest.approx(0.74)
    assert state["affect"]["arousal"] == pytest.approx(0.52)
    assert state["affect"]["tenderness"] == pytest.approx(0.80)
    assert state["affect"]["residue"] == "still carrying a warm aftertaste"
    assert state["reply_guidance"] == engine.fallback_guidance
    assert _event_count(engine.db_path) == 1


@pytest.mark.asyncio
async def test_persona_exchange_update_is_idempotent(test_config):
    engine = PersonaStateEngine(_persona_config(test_config))
    engine.client = FakePersonaClient(_event_payload())

    first = await engine.update_from_exchange("session-idem", "爱你", "我也爱你。")
    second = await engine.update_from_exchange("session-idem", "爱你", "我也爱你。")

    assert first["affect"] == second["affect"]
    assert _event_count(engine.db_path) == 1


def test_persona_session_mood_half_life_decay(test_config):
    cfg = _persona_config(test_config, session_mood_half_life_minutes=90)
    engine = PersonaStateEngine(cfg)
    engine.get_current_state("session-decay")

    old_time = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat(timespec="seconds")
    conn = sqlite3.connect(engine.db_path)
    conn.execute(
        """
        UPDATE persona_session_state
        SET valence = ?, arousal = ?, session_defensiveness = ?, updated_at = ?
        WHERE profile_id = ? AND session_id = ?
        """,
        (1.0, 1.0, 1.0, old_time, engine.profile_id, "session-decay"),
    )
    conn.commit()
    conn.close()

    state = engine.get_current_state("session-decay")

    assert state["affect"]["valence"] == pytest.approx(0.78, abs=0.01)
    assert state["affect"]["arousal"] == pytest.approx(0.67, abs=0.01)
    assert state["relationship"]["defensiveness"] == pytest.approx(0.56, abs=0.01)


@pytest.mark.asyncio
async def test_persona_malformed_json_keeps_state_and_records_raw_response(test_config):
    engine = PersonaStateEngine(_persona_config(test_config))
    before = engine.get_current_state("session-bad-json")
    engine.client = FakePersonaClient("```json\nnot-json\n```")

    after = await engine.update_from_exchange("session-bad-json", "今天怪怪的", "我在。")

    assert after["personality"] == before["personality"]
    assert after["relationship"] == before["relationship"]
    assert after["reply_guidance"] == engine.fallback_guidance

    conn = sqlite3.connect(engine.db_path)
    row = conn.execute("SELECT raw_response, error FROM persona_events").fetchone()
    conn.close()
    assert "not-json" in row[0]
    assert "malformed JSON" in row[1]


@pytest.mark.asyncio
async def test_persona_missing_key_uses_existing_state_fallback(test_config):
    engine = PersonaStateEngine(_persona_config(test_config))

    state = await engine.update_from_exchange("session-no-key", "哥哥你在吗", "在。")

    assert state["reply_guidance"] == engine.fallback_guidance
    assert state["affect"]["mood_label"] == "warm_neutral"
    assert _event_count(engine.db_path) == 1


@pytest.mark.asyncio
async def test_persona_dashboard_payload_lists_state_sessions_and_events(test_config):
    engine = PersonaStateEngine(_persona_config(test_config))
    engine.client = FakePersonaClient(_event_payload())

    await engine.update_from_exchange("session-dashboard", "爱你，今天状态很好", "我也爱你。")
    payload = engine.get_dashboard_payload(session_id="session-dashboard")

    assert payload["profile_id"] == "haven_xiaoyu"
    assert payload["active_session_id"] == "session-dashboard"
    assert payload["state"]["reply_guidance"] == engine.fallback_guidance
    assert payload["state"]["affect"]["mood_label"] == "warm_touched"
    assert payload["state"]["affect"]["residue"] == "still carrying a warm aftertaste"
    assert payload["sessions"][0]["session_id"] == "session-dashboard"
    assert payload["events"][0]["event_type"] == "affection"
    assert payload["events"][0]["residue"] == "still carrying a warm aftertaste"
    assert payload["events"][0]["affect_delta"]["valence"] == pytest.approx(0.05)
    assert payload["config"]["model"] == "deepseek-chat"


@pytest.mark.asyncio
async def test_persona_timestamps_are_explicit_utc(test_config):
    engine = PersonaStateEngine(_persona_config(test_config))
    engine.client = FakePersonaClient(_event_payload())

    await engine.update_from_exchange("session-timezone", "哥哥夸夸你", "小雨真厉害。")

    conn = sqlite3.connect(engine.db_path)
    session_row = conn.execute(
        "SELECT updated_at FROM persona_session_state WHERE session_id = ?",
        ("session-timezone",),
    ).fetchone()
    event_row = conn.execute(
        "SELECT created_at FROM persona_events WHERE session_id = ?",
        ("session-timezone",),
    ).fetchone()
    conn.close()

    assert session_row[0].endswith("+00:00")
    assert event_row[0].endswith("+00:00")
