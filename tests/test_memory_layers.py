from memory_layers import (
    DIRECT_CONTENT,
    DIRECT_EXPLICIT,
    DIRECT_EXPLICIT_OR_CONTENT,
    DIRECT_NEVER,
    LAYER_AFFECT_CONTEXT,
    LAYER_ANCHOR,
    LAYER_ARCHIVE,
    LAYER_CORE,
    LAYER_DYNAMIC,
    LAYER_FAVORITE,
    LAYER_RELATIONSHIP_WEATHER,
    LAYER_SOURCE_RECORD,
    RENDER_DIRECT_AUTO,
    RENDER_FAVORITE,
    RENDER_WEATHER,
    can_bucket_diffuse,
    can_moment_be_direct_seed,
    infer_bucket_layer,
    infer_moment_layer,
    is_context_only_section,
    normalize_write_classification,
    policy_for_bucket,
    policy_for_moment,
)


def _bucket(**metadata):
    return {
        "id": metadata.pop("id", "bucket-1"),
        "content": metadata.pop("content", "body"),
        "metadata": metadata,
    }


def _moment(section="body", **metadata):
    return {
        "bucket_id": metadata.pop("bucket_id", "bucket-1"),
        "section": section,
        "text": metadata.pop("text", "moment body"),
        "metadata": metadata,
    }


def test_core_layer_from_pinned_protected_or_permanent_bucket():
    assert infer_bucket_layer(_bucket(pinned=True, type="dynamic")) == LAYER_CORE
    assert infer_bucket_layer(_bucket(protected=True, type="dynamic")) == LAYER_CORE
    assert infer_bucket_layer(_bucket(type="permanent")) == LAYER_CORE

    policy = policy_for_bucket(_bucket(pinned=True))
    assert policy.direct_seed_policy == DIRECT_EXPLICIT_OR_CONTENT
    assert policy.gateway_section == "Core Memory"
    assert policy.can_diffuse is True


def test_anchor_layer_is_distinct_from_normal_dynamic_memory():
    bucket = _bucket(type="dynamic", anchor=True)

    assert infer_bucket_layer(bucket) == LAYER_ANCHOR
    assert policy_for_bucket(bucket).render_policy == RENDER_DIRECT_AUTO
    assert can_bucket_diffuse(bucket) is True


def test_relationship_weather_never_becomes_direct_seed_or_diffusion_source():
    bucket = _bucket(
        type="feel",
        tags=["relationship_weather", "daily_impression"],
    )
    moment = _moment(
        "body",
        bucket_type="feel",
        tags=["relationship_weather", "daily_impression"],
    )

    assert infer_bucket_layer(bucket) == LAYER_RELATIONSHIP_WEATHER
    assert policy_for_bucket(bucket).render_policy == RENDER_WEATHER
    assert policy_for_bucket(bucket).direct_seed_policy == DIRECT_NEVER
    assert can_bucket_diffuse(bucket) is False
    assert infer_moment_layer(moment) == LAYER_RELATIONSHIP_WEATHER
    assert can_moment_be_direct_seed(moment) is False


def test_context_only_sections_override_bucket_layer():
    for section in ("comment", "affect_anchor", "favorite_reason"):
        moment = _moment(
            section,
            bucket_type="dynamic",
            bucket_pinned=True,
            bucket_favorite=True,
            bucket_favorite_tags=["haven_favorite"],
        )
        assert infer_moment_layer(moment) == LAYER_AFFECT_CONTEXT
        assert policy_for_moment(moment).direct_seed_policy == DIRECT_NEVER
        assert can_moment_be_direct_seed(moment) is False

    assert is_context_only_section("affect_anchor") is True
    assert is_context_only_section("body") is False


def test_favorite_layer_uses_separate_policy_but_content_can_still_seed():
    bucket = _bucket(type="dynamic", tags=["haven_favorite", "flavor_soft"])
    moment = _moment(
        "body",
        bucket_type="dynamic",
        bucket_favorite=True,
        bucket_favorite_tags=["haven_favorite", "flavor_soft"],
    )

    assert infer_bucket_layer(bucket) == LAYER_FAVORITE
    assert policy_for_bucket(bucket).render_policy == RENDER_FAVORITE
    assert policy_for_bucket(bucket).direct_seed_policy == DIRECT_CONTENT
    assert infer_moment_layer(moment) == LAYER_FAVORITE
    assert can_moment_be_direct_seed(moment) is True


def test_archive_layer_only_allows_explicit_lookup():
    bucket = _bucket(type="dynamic", resolved=True)
    moment = _moment("body", bucket_type="dynamic", resolved=True)

    assert infer_bucket_layer(bucket) == LAYER_ARCHIVE
    assert policy_for_bucket(bucket).direct_seed_policy == DIRECT_EXPLICIT
    assert can_bucket_diffuse(bucket) is False
    assert can_moment_be_direct_seed(moment) is False
    assert can_moment_be_direct_seed(moment, explicit_lookup=True) is True


def test_source_record_is_not_normal_memory_context():
    bucket = _bucket(type="source", tags=["raw_source"])

    assert infer_bucket_layer(bucket) == LAYER_SOURCE_RECORD
    assert policy_for_bucket(bucket).direct_seed_policy == DIRECT_NEVER
    assert can_bucket_diffuse(bucket) is False


def test_default_dynamic_memory_is_direct_recallable_content():
    bucket = _bucket(type="dynamic")
    moment = _moment("body", bucket_type="dynamic")

    assert infer_bucket_layer(bucket) == LAYER_DYNAMIC
    assert policy_for_bucket(bucket).render_policy == RENDER_DIRECT_AUTO
    assert can_bucket_diffuse(bucket) is True
    assert infer_moment_layer(moment) == LAYER_DYNAMIC
    assert can_moment_be_direct_seed(moment) is True


def test_write_classification_accepts_model_fields():
    result = normalize_write_classification(
        memory_subject="relationship",
        memory_layer="relationship_lesson",
        tags=["relationship_event"],
        content="Haven 以后要先接住小雨的情绪。",
    )

    assert result == {
        "memory_subject": "relationship",
        "memory_layer": "relationship_lesson",
        "memory_classification_source": "model",
    }


def test_write_classification_hard_tags_override_model_fields():
    result = normalize_write_classification(
        memory_subject="event",
        memory_layer="process_event",
        tags=["boundary"],
        content="小雨不喜欢被说教。",
    )

    assert result == {
        "memory_subject": "user",
        "memory_layer": "stable_boundary",
        "memory_classification_source": "model_adjusted",
    }


def test_write_classification_falls_back_to_rules():
    project = normalize_write_classification(tags=["project_event"], content="p0 还在测试。")
    state = normalize_write_classification(content="小雨今天头疼。")

    assert project["memory_subject"] == "event"
    assert project["memory_layer"] == "process_event"
    assert project["memory_classification_source"] == "rule"
    assert state["memory_subject"] == "user"
    assert state["memory_layer"] == "short_state"
