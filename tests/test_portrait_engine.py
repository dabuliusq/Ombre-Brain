import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from portrait_engine import DailyPortraitMaintainer


@pytest.mark.asyncio
async def test_daily_portrait_maintainer_writes_evidence_bound_state_only(tmp_path, test_config, bucket_mgr):
    evidence_id = await bucket_mgr.create(
        content=(
            "开头原文有一点松动味道，不能只靠短 moment 丢掉。\n\n"
            "### moment\n\n"
            "小雨说最近在把新窗口 handoff 改成画像和近期状态，而不是塞一堆旧记忆。\n\n"
            "### assistant_reflection\n\n"
            "Haven 要把换窗恢复做得轻一点，像醒来，不像翻档案。"
        ),
        name="portrait handoff 方向",
        tags=["project_event"],
        domain=["记忆系统"],
        created="2026-06-07T10:00:00+08:00",
        updated_at="2026-06-07T10:00:00+08:00",
    )
    await bucket_mgr.create(
        content="这条 pinned 不应该被画像维护器自动维护。",
        name="核心规则",
        tags=["core"],
        domain=["规则"],
        pinned=True,
        created="2026-06-07T10:00:00+08:00",
        updated_at="2026-06-07T10:00:00+08:00",
    )
    state_path = tmp_path / "state" / "portrait_state.json"
    cfg = {
        **test_config,
        "portrait": {
            "enabled": True,
            "auto_enabled": True,
            "daily_enabled": True,
            "state_path": str(state_path),
            "material_limit": 8,
            "first_run_material_limit": 8,
        },
    }
    engine = DailyPortraitMaintainer(cfg)

    async def fake_patch(date_key, state, materials, *, initial):
        assert initial is True
        assert [item["bucket_id"] for item in materials["buckets"]] == [evidence_id]
        assert materials["buckets"][0]["path"].endswith(".md")
        assert [item["heading"] for item in materials["buckets"][0]["key_sections"]] == [
            "moment",
            "assistant_reflection",
        ]
        assert "开头原文有一点松动味道" in materials["buckets"][0]["source_excerpt"]
        return {
            "daily_summary": "小雨把换窗恢复方向定到画像和近期状态。",
            "add_recent": [
                {
                    "scope": "user",
                    "text": "小雨正在推进 handoff 画像化，目标是少 token 且更像醒来。",
                    "evidence": [{"bucket_id": evidence_id}],
                    "confidence": 0.82,
                }
            ],
            "move_to_staging": [
                {
                    "scope": "relationship",
                    "text": "换窗连续性优先恢复身份、关系和近期正在做的事。",
                    "evidence": [{"bucket_id": evidence_id}],
                    "confidence": 0.78,
                }
            ],
            "rewrite_mid_term": [
                {
                    "scope": "relationship",
                    "text": "换窗连续性优先恢复身份、关系和近期正在做的事。",
                    "evidence": [{"bucket_id": evidence_id}],
                    "confidence": 0.78,
                }
            ],
            "stable_candidate": [
                {
                    "scope": "relationship",
                    "text": "新窗口不应该依赖广泛旧记忆堆叠。",
                    "evidence": [{"bucket_id": evidence_id}],
                    "confidence": 0.76,
                }
            ],
            "profile_fact_candidate": [
                {
                    "scope": "user",
                    "text": "小雨偏好换窗时先恢复画像和最近事项。",
                    "profile_kind": "preference",
                    "predicate": "handoff_context_shape",
                    "evidence": [{"bucket_id": evidence_id}],
                    "confidence": 0.74,
                }
            ],
            "skip": [],
        }

    engine._generate_patch = fake_patch

    result = await engine.maintain_daily(
        bucket_mgr,
        force=True,
        now=datetime(2026, 6, 7, 23, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert result["status"] == "initialized"
    assert result["patch_counts"]["add_recent"] == 1
    assert state_path.exists()

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_run_date"] == "2026-06-07"
    assert state["daily_summaries"] == {}
    assert state["portrait"]["user"]["recent_buffer"][0]["evidence"] == [{"bucket_id": evidence_id}]
    assert state["portrait"]["relationship"]["staging_pool"][0]["evidence"] == [{"bucket_id": evidence_id}]
    assert state["portrait"]["relationship"]["mid_term_evidence"] == [{"bucket_id": evidence_id}]
    assert state["portrait"]["relationship"]["stable"] == ""
    assert state["stable_candidates"][0]["status"] == "candidate"
    assert state["profile_fact_candidates"][0]["status"] == "candidate"

    all_buckets = await bucket_mgr.list_all(include_archive=True)
    assert len(all_buckets) == 2


@pytest.mark.asyncio
async def test_daily_portrait_initial_run_requires_manual_force_by_default(tmp_path, test_config, bucket_mgr):
    await bucket_mgr.create(
        content="### moment\n\n小雨决定画像第一次要手动生成，避免别人更新代码后自动跑。",
        name="portrait manual first run",
        tags=["project_event"],
        domain=["记忆系统"],
        created="2026-06-07T10:00:00+08:00",
        updated_at="2026-06-07T10:00:00+08:00",
    )
    state_path = tmp_path / "state" / "portrait_state.json"
    cfg = {
        **test_config,
        "portrait": {
            "enabled": True,
            "auto_enabled": True,
            "daily_enabled": True,
            "state_path": str(state_path),
        },
    }
    engine = DailyPortraitMaintainer(cfg)

    async def fail_patch(*_args, **_kwargs):
        raise AssertionError("initial auto run should not generate a patch")

    engine._generate_patch = fail_patch
    result = await engine.maintain_daily(
        bucket_mgr,
        force=False,
        now=datetime(2026, 6, 7, 23, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "initial_requires_manual"
    assert result["initial"] is True
    assert not state_path.exists()

    async def fake_patch(date_key, state, materials, *, initial):
        assert initial is True
        assert len(materials["buckets"]) == 1
        return {
            "daily_summary": "画像第一次由手动生成。",
            "add_recent": [
                {
                    "scope": "relationship",
                    "text": "画像第一次初始化需要小雨手动触发。",
                    "evidence": [{"bucket_id": materials["buckets"][0]["bucket_id"]}],
                    "confidence": 0.8,
                }
            ],
            "move_to_staging": [],
            "rewrite_mid_term": [],
            "stable_candidate": [],
            "profile_fact_candidate": [],
            "skip": [],
        }

    engine._generate_patch = fake_patch
    forced = await engine.maintain_daily(
        bucket_mgr,
        force=True,
        now=datetime(2026, 6, 7, 23, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert forced["status"] == "initialized"
    assert forced["initial"] is True
    assert state_path.exists()


@pytest.mark.asyncio
async def test_daily_portrait_can_auto_initial_when_enabled(tmp_path, test_config, bucket_mgr):
    await bucket_mgr.create(
        content="### moment\n\n测试显式开启 auto_initial_enabled 时允许定时器初始化。",
        name="portrait auto initial opt in",
        tags=["project_event"],
        domain=["记忆系统"],
        created="2026-06-07T10:00:00+08:00",
        updated_at="2026-06-07T10:00:00+08:00",
    )
    state_path = tmp_path / "state" / "portrait_state.json"
    cfg = {
        **test_config,
        "portrait": {
            "enabled": True,
            "auto_enabled": True,
            "auto_initial_enabled": True,
            "daily_enabled": True,
            "state_path": str(state_path),
        },
    }
    engine = DailyPortraitMaintainer(cfg)

    async def fake_patch(date_key, state, materials, *, initial):
        assert initial is True
        return {
            "daily_summary": "显式开启后，自动首次生成被允许。",
            "add_recent": [],
            "move_to_staging": [],
            "rewrite_mid_term": [],
            "stable_candidate": [],
            "profile_fact_candidate": [],
            "skip": [],
        }

    engine._generate_patch = fake_patch
    result = await engine.maintain_daily(
        bucket_mgr,
        force=False,
        now=datetime(2026, 6, 7, 23, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert result["status"] == "initialized"
    assert result["initial"] is True
    assert state_path.exists()


@pytest.mark.asyncio
async def test_daily_portrait_skips_date_already_present_in_runs(tmp_path, test_config, bucket_mgr):
    await bucket_mgr.create(
        content="### moment\n\n已有同一天画像 run 时，非 force 不应再生成。",
        name="portrait duplicate date guard",
        tags=["project_event"],
        domain=["记忆系统"],
        created="2026-06-07T10:00:00+08:00",
        updated_at="2026-06-07T10:00:00+08:00",
    )
    state_path = tmp_path / "state" / "portrait_state.json"
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "auto_enabled": True,
                "daily_enabled": True,
                "state_path": str(state_path),
            },
        }
    )
    state = engine._empty_state()
    state["last_run_date"] = "2026-06-06"
    state["runs"].append(
        {
            "date": "2026-06-07",
            "created_at": "2026-06-07T03:00:00+00:00",
            "initial": True,
            "material_count": 1,
            "persona_event_count": 0,
            "patch_counts": {},
            "rejected_count": 0,
            "model": "test",
        }
    )
    engine.save_state(state)

    async def fail_patch(*_args, **_kwargs):
        raise AssertionError("existing run date should not regenerate")

    engine._generate_patch = fail_patch
    result = await engine.maintain_daily(
        bucket_mgr,
        force=False,
        now=datetime(2026, 6, 7, 23, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert result["status"] == "exists"
    assert result["date"] == "2026-06-07"


@pytest.mark.asyncio
async def test_portrait_run_due_does_not_backfill_before_newer_manual_run(tmp_path, test_config, bucket_mgr):
    state_path = tmp_path / "state" / "portrait_state.json"
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "auto_enabled": True,
                "daily_enabled": True,
                "daily_hour": 4,
                "state_path": str(state_path),
            },
        }
    )
    state = engine._empty_state()
    state["last_run_date"] = "2026-06-07"
    state["runs"].append(
        {
            "date": "2026-06-07",
            "created_at": "2026-06-07T03:00:00+00:00",
            "initial": True,
            "material_count": 160,
            "persona_event_count": 24,
            "patch_counts": {},
            "rejected_count": 0,
            "model": "test",
        }
    )
    engine.save_state(state)
    engine._local_now = lambda now=None: datetime(2026, 6, 7, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    async def fail_daily(*_args, **_kwargs):
        raise AssertionError("scheduler should not run an older daily target after a newer manual run")

    engine.maintain_daily = fail_daily

    assert await engine.run_due(bucket_mgr) == []


def test_portrait_mid_term_rewrite_requires_staging_evidence(tmp_path, test_config):
    state_path = tmp_path / "state" / "portrait_state.json"
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "state_path": str(state_path),
            },
        }
    )
    previous = engine._portrait_snapshot(engine._empty_state())
    materials = {
        "buckets": [{"bucket_id": "fresh-bucket"}],
        "persona_events": [],
        "previous_portrait": previous,
    }

    normalized, rejected = engine._normalize_patch(
        {
            "rewrite_mid_term": [
                {
                    "scope": "relationship",
                    "text": "这条不能直接从当天新材料写成 mid-term。",
                    "evidence": [{"bucket_id": "fresh-bucket"}],
                }
            ]
        },
        materials,
    )

    assert normalized["rewrite_mid_term"] == []
    assert rejected[0]["reason"] == "missing_staging_evidence"

    normalized, rejected = engine._normalize_patch(
        {
            "move_to_staging": [
                {
                    "scope": "relationship",
                    "text": "先放入 staging 的观察。",
                    "evidence": [{"bucket_id": "fresh-bucket"}],
                }
            ],
            "rewrite_mid_term": [
                {
                    "scope": "relationship",
                    "text": "这条可以从本次 staging 证据综合。",
                    "evidence": [{"bucket_id": "fresh-bucket"}],
                }
            ],
        },
        materials,
    )

    assert rejected == []
    assert normalized["move_to_staging"][0]["evidence"] == [{"bucket_id": "fresh-bucket"}]
    assert normalized["rewrite_mid_term"][0]["evidence"] == [{"bucket_id": "fresh-bucket"}]


def test_handoff_recent_continuity_sorts_equal_timestamps_without_dict_compare(tmp_path, test_config):
    state_path = tmp_path / "state" / "portrait_state.json"
    engine = DailyPortraitMaintainer(
        {
            **test_config,
            "portrait": {
                "enabled": True,
                "state_path": str(state_path),
            },
        }
    )
    state = engine._empty_state()
    same_time = "2026-06-07T01:25:42+00:00"
    state["portrait"]["user"]["recent_buffer"].append(
        {
            "text": "小雨最近在调整换窗 handoff。",
            "evidence": [{"bucket_id": "u"}],
            "updated_at": same_time,
        }
    )
    state["portrait"]["relationship"]["recent_buffer"].append(
        {
            "text": "关系画像要优先于旧记忆堆。",
            "evidence": [{"bucket_id": "r"}],
            "updated_at": same_time,
        }
    )
    engine.save_state(state)

    sections = engine.build_handoff_sections(max_recent_items=4)

    assert "小雨最近在调整换窗 handoff" in sections["recent_continuity"]
    assert "关系画像要优先于旧记忆堆" in sections["recent_continuity"]
