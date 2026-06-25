from memory_metadata import normalize_memory_metadata


def test_normalize_memory_metadata_splits_domain_kind_status_and_flags():
    bucket = {
        "id": "b1",
        "path": "buckets/dynamic/AI/example.md",
        "metadata": {
            "name": "Gateway recall 修复",
            "domain": ["AI", "未解决"],
            "tags": ["gateway", "source_record"],
            "type": "dynamic",
            "resolved": False,
            "pinned": True,
        },
    }

    view = normalize_memory_metadata(bucket)

    assert view == {
        "canonical_domain": "ai_tools",
        "kind": "source_record",
        "status_view": "protected",
        "flags": ["pinned", "source_record"],
        "legacy_domain": ["AI", "未解决"],
    }


def test_normalize_memory_metadata_prefers_existing_canonical_fields_without_mutating_bucket():
    bucket = {
        "id": "b2",
        "metadata": {
            "canonical_domain": "project_code",
            "kind": "profile_fact",
            "status": "digested",
            "domain": ["恋爱", "代码"],
            "tags": ["favorite"],
        },
    }
    original_domain = list(bucket["metadata"]["domain"])

    view = normalize_memory_metadata(bucket)

    assert view["canonical_domain"] == "project_code"
    assert view["kind"] == "profile_fact"
    assert view["status_view"] == "digested"
    assert view["flags"] == ["favorite", "profile_fact"]
    assert bucket["metadata"]["domain"] == original_domain


def test_normalize_memory_metadata_keeps_scene_out_of_domain():
    task_bucket = {
        "metadata": {
            "domain": ["亲密", "代码"],
            "tags": ["relationship_tone"],
            "type": "dynamic",
        }
    }

    view = normalize_memory_metadata(task_bucket)

    assert view["canonical_domain"] == "intimacy"
    assert view["kind"] == "event"
    assert view["status_view"] == "active"
    assert view["legacy_domain"] == ["亲密", "代码"]
