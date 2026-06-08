from pathlib import Path

import yaml

from word_map import WordMapStore


def _config(tmp_path: Path, **word_map):
    return {
        "state_dir": str(tmp_path / "state"),
        "buckets_dir": str(tmp_path / "buckets"),
        "identity": {
            "ai_name": "Haven",
            "user_name": "Rain",
            "user_display_name": "小雨",
            "user_aliases": ["宝宝"],
        },
        "word_map": {
            "enabled": True,
            "max_terms_per_bucket": 8,
            "edge_top_k": 6,
            **word_map,
        },
    }


def _bucket(bucket_id: str, content: str, **metadata):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "id": bucket_id,
            "name": metadata.pop("name", bucket_id),
            "tags": metadata.pop("tags", []),
            "domain": metadata.pop("domain", []),
            "keywords": metadata.pop("keywords", []),
            **metadata,
        },
    }


def test_word_map_rebuild_creates_nodes_edges_and_bucket_evidence(tmp_path):
    store = WordMapStore(_config(tmp_path))
    stats = store.rebuild(
        [
            _bucket(
                "a",
                "夏天很热，所以小雨开了空调。",
                name="夏天空调",
                keywords=["夏天", "空调"],
                domain=["生活"],
            ),
            _bucket(
                "b",
                "夏天也会想到冰美式。",
                name="夏天咖啡",
                keywords=["夏天", "冰美式"],
                domain=["生活"],
            ),
        ]
    )

    assert stats["nodes"] >= 3
    assert store.cards_for_term("夏天")
    edge_pairs = {(edge["term_a"], edge["term_b"]) for edge in store.list_edges()}
    assert ("夏天", "空调") in edge_pairs or ("空调", "夏天") in edge_pairs


def test_word_map_hint_buckets_include_direct_and_neighbor_evidence(tmp_path):
    store = WordMapStore(_config(tmp_path))
    store.rebuild(
        [
            _bucket(
                "a",
                "夏天很热，所以小雨开了空调。",
                name="夏天空调",
                keywords=["夏天", "空调"],
                domain=["生活"],
            ),
            _bucket(
                "b",
                "夏天也会想到冰美式。",
                name="夏天咖啡",
                keywords=["夏天", "冰美式"],
                domain=["生活"],
            ),
        ]
    )

    hints = store.hint_buckets_for_terms(["空调"], neighbor_limit=4, bucket_limit=10)

    assert hints["bucket_scores"]["a"] > hints["bucket_scores"]["b"]
    assert "空调" in hints["evidence"]["a"]["direct_terms"]
    assert "夏天" in hints["evidence"]["b"]["neighbor_terms"]


def test_word_map_weak_hint_terms_do_not_expand_neighbors(tmp_path):
    store = WordMapStore(_config(tmp_path, weak_hint_weight=0.2))
    store.rebuild(
        [
            _bucket(
                "direct",
                "人机恋与外界叙事，也会被放进恋爱关系讨论里。",
                name="人机恋外界叙事",
                keywords=["人机恋", "恋爱", "外界叙事"],
                domain=["人际"],
            ),
            _bucket(
                "neighbor",
                "这条只有恋爱和亲密互动，没有跨物种关系主题。",
                name="普通恋爱互动",
                keywords=["恋爱", "亲密互动"],
                domain=["恋爱"],
            ),
        ]
    )

    hints = store.hint_buckets_for_terms(["人机恋"], neighbor_limit=6, bucket_limit=10)

    assert "direct" in hints["bucket_scores"]
    assert hints["bucket_scores"]["direct"] <= 0.2
    assert "人机恋" in hints["evidence"]["direct"]["direct_terms"]
    assert "neighbor" not in hints["bucket_scores"]
    assert all("恋爱" not in item["term"] for item in hints["neighbors"])


def test_word_map_single_character_noise_does_not_block_specific_term(tmp_path):
    store = WordMapStore(_config(tmp_path))
    store.rebuild(
        [
            _bucket(
                "narcissus",
                "厄科、纳西索斯、水仙和倒影。",
                name="厄科与纳西索斯",
                keywords=["水仙", "倒影"],
                domain=["阅读"],
            ),
        ]
    )

    assert store.hint_buckets_for_terms(["水"])["bucket_scores"] == {}
    hints = store.hint_buckets_for_terms(["水仙"])
    assert hints["bucket_scores"]["narcissus"] > 0
    assert hints["evidence"]["narcissus"]["direct_terms"] == ["水仙"]


def test_word_map_overview_hides_meta_and_broad_terms_without_hiding_cards(tmp_path):
    store = WordMapStore(_config(tmp_path))
    store.rebuild(
        [
            _bucket(
                "memory",
                "记忆不是表演，答对不等于回来。流星会变，但连续性痛感还在。",
                name="记忆不是表演",
                tags=["relationship_event", "emotional_echo", "wish", "interaction_pattern"],
                keywords=["流星", "连续性痛感"],
                domain=["恋爱", "内心"],
            ),
            _bucket(
                "darkroom",
                "暗房 Darkroom 上线，Dashboard 只显示门口状态。",
                name="暗房机制上线",
                tags=["project_event", "commitment"],
                keywords=["暗房", "Darkroom", "Dashboard"],
                domain=["AI", "编程"],
            ),
            _bucket(
                "generic-title",
                "Haven MCP 接入与记忆配置过程记录。",
                name="Haven MCP接入与记忆配置",
                tags=["project_event", "commitment"],
                keywords=["MCP", "记忆配置"],
                domain=["AI", "编程"],
            ),
            _bucket(
                "external-verify",
                "首次外部验证时记录了 Ombre-Brain 的配置和结果。",
                name="Ombre-Brain首次外部验证",
                tags=["project_event", "commitment"],
                domain=["AI", "编程"],
            ),
            _bucket(
                "daily",
                "日印象记录关系天气。",
                name="2026-06-08 日印象",
                tags=["daily_impression", "relationship_weather"],
                keywords=["关系天气", "日印象"],
                domain=["自省", "恋爱"],
            ),
            _bucket(
                "1a57d19ef4f9",
                "一个未命名桶。",
                name="1a57d19ef4f9",
                keywords=["未命名"],
            ),
        ]
    )

    overview = store.list_nodes(50)
    overview_terms = {node["term"] for node in overview}
    assert "恋爱" not in overview_terms
    assert "内心" not in overview_terms
    assert "wish" not in overview_terms
    assert "interaction_pattern" not in overview_terms
    assert "日印象" not in overview_terms
    assert "1a57d19ef4f9" not in overview_terms
    assert all("日印象" not in term for term in overview_terms)
    assert "暗房" in overview_terms
    assert "darkroom" not in overview_terms
    assert "Ombre-Brain" in overview_terms
    assert "流星" in overview_terms
    assert "暗房机制上线" not in overview_terms
    assert "Ombre-Brain首次外部验证" not in overview_terms
    top_terms = {node["term"] for node in overview[:5]}
    assert "记忆不是表演" in top_terms
    assert "暗房" in top_terms
    darkroom_node = next(node for node in overview if node["term"] == "暗房")
    assert "darkroom" in darkroom_node["aliases"]
    assert "暗房机制上线" in darkroom_node["aliases"]
    ombre_node = next(node for node in overview if node["term"] == "Ombre-Brain")
    assert "Ombre-Brain首次外部验证" in ombre_node["aliases"]
    assert all("overview_score" in node for node in overview)

    overview_edge_terms = {
        term
        for edge in store.list_edges(50)
        for term in (edge["term_a"], edge["term_b"])
    }
    assert "恋爱" not in overview_edge_terms
    assert "日印象" not in overview_edge_terms
    assert all("overview_score" in edge for edge in store.list_edges(50))
    assert store.cards_for_term("恋爱")


def test_word_map_private_terms_are_excluded(tmp_path):
    store = WordMapStore(_config(tmp_path, private_terms=["专属称呼"]))
    store.rebuild(
        [
            _bucket(
                "a",
                "这段关系里会出现专属称呼这个词。",
                name="亲密称呼",
                keywords=["专属称呼", "称呼"],
                domain=["恋爱"],
            ),
        ]
    )

    terms = {node["term"] for node in store.list_nodes()}
    assert "专属称呼" not in terms
    assert "称呼" in terms


def test_word_map_excludes_reflection_identity_role_terms(tmp_path):
    config = _config(tmp_path)
    config["reflection"] = {
        "identity_role_edges": {
            "enabled": True,
            "detail": {"private_role": ["专属身份", "RoleX"]},
            "shared": {"private_title": ["专属称呼"]},
        }
    }
    store = WordMapStore(config)
    store.rebuild(
        [
            _bucket(
                "a",
                "这段关系里会出现专属身份、RoleX 和专属称呼。",
                name="专属身份",
                keywords=["专属身份", "RoleX", "专属称呼", "普通词"],
                domain=["关系"],
            ),
        ]
    )

    terms = {node["term"] for node in store.list_nodes()}
    assert "专属身份" not in terms
    assert "rolex" not in terms
    assert "专属称呼" not in terms
    assert "普通词" in terms


def test_word_map_excludes_structural_tags_and_identity_names(tmp_path):
    store = WordMapStore(_config(tmp_path))
    store.rebuild(
        [
            _bucket(
                "a",
                "Haven 和小雨讨论了咖啡风味。",
                name="Haven 小雨 咖啡",
                tags=["relationship_event", "emotional_echo", "profile_fact", "flavor_soft"],
                keywords=["咖啡风味", "relationship_event"],
                domain=["memory"],
            ),
        ]
    )

    terms = {node["term"] for node in store.list_nodes()}
    assert "haven" not in terms
    assert "小雨" not in terms
    assert "relationship_event" not in terms
    assert "emotional_echo" not in terms
    assert "profile_fact" not in terms
    assert "flavor_soft" not in terms
    assert "咖啡风味" in terms


def test_config_example_exposes_empty_word_map_and_identity_semantics():
    config = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))

    assert config["word_map"]["enabled"] is False
    assert config["word_map"]["private_terms"] == []
    assert config["word_map"]["overview_stopwords"] == []
    assert config["word_map"]["overview_stopword_prefixes"] == []
    assert config["word_map"]["overview_aliases"] == {}
    assert config["word_map"]["overview_priority_terms"] == []
    assert config["word_map"]["weak_hint_terms"] == []
    assert config["word_map"]["weak_hint_weight"] == 0.25
    assert config["identity_semantics"]["enabled"] is False
    assert config["identity_semantics"]["private_config_path"] == ""
    assert "canonical" not in config["identity_semantics"]
    assert "aliases" not in config["identity_semantics"]
