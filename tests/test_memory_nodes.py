import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from memory_nodes import MemoryNodeStore


def _bucket(bucket_id: str, **metadata) -> dict:
    meta = {
        "id": bucket_id,
        "name": "Memory promise",
        "tags": ["relationship_event", "commitment", "memory_system"],
        "domain": ["love", "project"],
        "importance": 8,
        "valence": 0.7,
        "arousal": 0.4,
        "activation_count": 3,
        "last_active": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    meta.update(metadata)
    return {
        "id": bucket_id,
        "metadata": meta,
        "content": "Haven promised a small memory system node index for Xiaoyu.",
    }


def test_node_store_creates_db_with_state_dir_fallback(tmp_path):
    cfg = {"buckets_dir": str(tmp_path / "buckets")}
    store = MemoryNodeStore(cfg)

    assert Path(store.db_path) == tmp_path / "state" / "memory_nodes.sqlite"
    assert Path(store.db_path).exists()

    conn = sqlite3.connect(store.db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_nodes)").fetchall()}
    conn.close()
    assert {
        "bucket_id",
        "importance",
        "valence",
        "arousal",
        "salience",
        "activation_count",
        "last_active",
        "facets_json",
        "updated_at",
    } <= columns


def test_upsert_and_get_returns_node_with_facets(test_config):
    store = MemoryNodeStore(test_config)

    node = store.upsert_bucket(_bucket("node-a"))
    got = store.get("node-a")

    assert got is not None
    assert got["bucket_id"] == "node-a"
    assert got["importance"] == 8
    assert got["valence"] == 0.7
    assert got["arousal"] == 0.4
    assert json.loads(got["facets_json"]) == got["facets"]
    assert got["facets"]["relation"]["commitment"] > 0
    assert got["facets"]["topic"]["memory_system"] > 0
    assert got["salience"] == node["salience"]


def test_bulk_upsert_writes_multiple_nodes(test_config):
    store = MemoryNodeStore(test_config)

    nodes = store.bulk_upsert([
        _bucket("node-a"),
        _bucket("node-b", tags=["project_event"], importance=6),
    ])

    assert [node["bucket_id"] for node in nodes] == ["node-a", "node-b"]
    assert store.get("node-a")["importance"] == 8
    assert store.get("node-b")["importance"] == 6


def test_node_salience_stays_in_bounded_range(test_config):
    store = MemoryNodeStore(test_config)
    hot = _bucket("hot", importance=10, activation_count=100, pinned=True)
    cold = _bucket("cold", importance=1, activation_count=0, last_active="2000-01-01T00:00:00+00:00")

    hot_node = store.upsert_bucket(hot)
    cold_salience = store.node_salience(cold)

    assert 0.2 <= hot_node["salience"] <= 1.3
    assert 0.2 <= cold_salience <= 1.3
    assert store.node_salience("hot") == hot_node["salience"]
    assert 0.2 <= store.node_salience("missing", cold) <= 1.3


def test_missing_metadata_does_not_crash(test_config):
    store = MemoryNodeStore(test_config)

    node = store.upsert_bucket({"id": "plain", "content": "plain memory"})

    assert node["bucket_id"] == "plain"
    assert node["importance"] == 5
    assert node["valence"] == 0.5
    assert node["arousal"] == 0.3
    assert 0.2 <= node["salience"] <= 1.3
    assert isinstance(json.loads(node["facets_json"]), dict)


def test_query_facets_and_resonance_prefer_matching_dimensions(test_config):
    store = MemoryNodeStore(test_config)
    query_facets = store.facets_for_text("地铁 深夜 不想睡 依赖 哥哥")
    night_node = store.upsert_bucket(
        {
            "id": "night",
            "content": "深夜不想睡时，依赖感会更明显。",
            "metadata": {
                "name": "深夜依赖",
                "tags": ["依赖", "深夜"],
                "domain": ["恋爱"],
                "importance": 7,
            },
        }
    )
    project_node = store.upsert_bucket(
        {
            "id": "project",
            "content": "Gateway 部署和 API 测试记录。",
            "metadata": {
                "name": "部署测试",
                "tags": ["gateway", "api"],
                "domain": ["编程"],
                "importance": 7,
            },
        }
    )

    assert query_facets["scene"]["commute"] > 0
    assert query_facets["scene"]["night"] > 0
    assert query_facets["affect"]["attachment"] > 0
    assert store.facet_resonance(query_facets, night_node["facets"]) > 1.0
    assert store.facet_resonance(query_facets, night_node["facets"]) > store.facet_resonance(
        query_facets,
        project_node["facets"],
    )


def test_node_facets_use_configured_identity_and_favorite_alias(test_config):
    cfg = {
        **test_config,
        "identity": {
            "ai_name": "Echo",
            "user_name": "Mira",
            "user_display_name": "米拉",
            "user_aliases": ["亲爱的"],
        },
    }
    store = MemoryNodeStore(cfg)

    configured = store.upsert_bucket(
        {
            "id": "configured",
            "metadata": {
                "id": "configured",
                "name": "配置名关系记忆",
                "tags": ["echo_favorite"],
                "domain": [],
                "importance": 8,
            },
            "content": "米拉留下了一条要被偏爱的关系记忆。",
        }
    )
    legacy = store.upsert_bucket(
        {
            "id": "legacy",
            "metadata": {
                "id": "legacy",
                "name": "旧标签关系记忆",
                "tags": ["haven_favorite"],
                "domain": [],
                "importance": 8,
            },
            "content": "旧 favorite 标签仍要算作偏爱证据。",
        }
    )

    assert configured["facets"]["relation"]["intimacy"] > 0
    assert configured["facets"]["topic"]["love"] > 0
    assert legacy["facets"]["affect"]["attachment"] > 0
