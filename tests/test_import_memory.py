import pytest

from import_memory import IMPORT_EXTRACT_PROMPT, ImportEngine, chunk_turns, detect_and_parse
from utils import count_tokens_approx


class DummyDehydrator:
    api_available = True
    model = "dummy"

    async def merge(self, old_content: str, new_content: str) -> str:
        return f"{old_content}\n{new_content}"


def test_markdown_parser_supports_chinese_role_prefixes():
    text = "用户：这里有 <b>HTML</b> & 符号\n助手：收到，我不会改写原文。"

    turns = detect_and_parse(text, "chat.md")

    assert [(turn["role"], turn["content"]) for turn in turns] == [
        ("user", "这里有 <b>HTML</b> & 符号"),
        ("assistant", "收到，我不会改写原文。"),
    ]


def test_markdown_parser_supports_ascii_role_prefixes():
    text = "user: first\nassistant: second\nHuman: third\n**AI:** fourth"

    turns = detect_and_parse(text, "chat.md")

    assert [(turn["role"], turn["content"]) for turn in turns] == [
        ("user", "first"),
        ("assistant", "second"),
        ("user", "third"),
        ("assistant", "fourth"),
    ]


def test_oversized_markdown_turn_is_split_into_multiple_chunks():
    long_line = "这是一段很长的导入文本。" * 700
    turns = detect_and_parse("用户：" + long_line, "chat.md")

    chunks = chunk_turns(turns, target_tokens=800)

    assert len(chunks) > 1
    assert all(count_tokens_approx(chunk["content"]) <= 1200 for chunk in chunks)
    assert chunks[0]["content"].startswith("[用户] ")
    assert all("[上下文提示]" in chunk["content"] for chunk in chunks[1:])
    assert all("[本段内容]" in chunk["content"] for chunk in chunks[1:])


def test_oversized_markdown_overlap_is_marked_as_context_only():
    long_turn = "\n".join(
        f"第{i}段：这是一段需要连续理解的内容。" * 20
        for i in range(20)
    )
    turns = detect_and_parse("用户：" + long_turn, "chat.md")

    chunks = chunk_turns(turns, target_tokens=500)

    assert len(chunks) > 1
    assert "请不要从这里单独提取记忆" in chunks[1]["content"]
    assert chunks[1]["content"].index("[上下文提示]") < chunks[1]["content"].index("[本段内容]")


def test_import_prompt_caps_items_and_tags_for_large_imports():
    assert "总条目数控制在 0~5 个" in IMPORT_EXTRACT_PROMPT
    assert "tags 最多 6 个，每个不超过 12 个字" in IMPORT_EXTRACT_PROMPT
    assert "总条目数控制在 0~10 个" not in IMPORT_EXTRACT_PROMPT


def test_import_extraction_keeps_output_budget_and_does_not_truncate_input():
    class DummyClient:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    DummyClient.kwargs = kwargs

                    class Message:
                        content = "[]"

                    class Choice:
                        message = Message()

                    class Response:
                        choices = [Choice()]

                    return Response()

    class DummyDehydrator:
        api_available = True
        client = DummyClient()
        model = "dummy"

    engine = ImportEngine(
        {"buckets_dir": "buckets"},
        bucket_mgr=None,
        dehydrator=DummyDehydrator(),
        embedding_engine=None,
    )

    import asyncio

    asyncio.run(engine._extract_memories("hello"))

    assert DummyClient.kwargs["max_tokens"] == 4096
    long_chunk = "这是一段需要完整送入模型的导入文本。" * 800
    asyncio.run(engine._extract_memories(long_chunk))
    assert DummyClient.kwargs["messages"][1]["content"] == long_chunk


@pytest.mark.asyncio
async def test_import_dedupes_existing_bucket_by_content(test_config, bucket_mgr):
    content = "小雨决定周末去杭州参加朋友婚礼，需要提前买高铁票并准备蓝色连衣裙。"
    await bucket_mgr.create(
        content=content,
        name="婚礼安排",
        domain=["人际"],
        tags=["婚礼", "杭州"],
    )
    engine = ImportEngine(test_config, bucket_mgr, DummyDehydrator())

    status = await engine._merge_or_create_item(
        {
            "name": "出行计划",
            "content": content,
            "domain": ["事务"],
            "tags": ["高铁", "连衣裙"],
            "importance": 6,
        }
    )

    buckets = await bucket_mgr.list_all(include_archive=False)
    assert status == "duplicate"
    assert len(buckets) == 1


@pytest.mark.asyncio
async def test_import_process_counts_duplicate_separately_from_merge(test_config, bucket_mgr):
    content = "小雨决定周末去杭州参加朋友婚礼，需要提前买高铁票并准备蓝色连衣裙。"
    await bucket_mgr.create(content=content, name="婚礼安排", domain=["人际"])
    engine = ImportEngine(test_config, bucket_mgr, DummyDehydrator())

    async def fake_extract(_chunk_content):
        return [
            {
                "name": "重复婚礼",
                "content": content,
                "domain": ["人际"],
                "tags": ["婚礼"],
                "importance": 6,
            }
        ]

    engine._extract_memories = fake_extract
    await engine._process_single_chunk(
        {
            "content": "chunk",
            "source_chunk_id": "hash:00001",
            "source_file": "chat.md",
            "source_hash": "hash",
            "chunk_index": 1,
            "chunk_total": 1,
            "turn_count": 1,
        },
        preserve_raw=False,
    )

    assert engine.state.data["memories_duplicate_skipped"] == 1
    assert engine.state.data["memories_merged"] == 0


@pytest.mark.asyncio
async def test_import_dedupes_existing_bucket_by_similar_body(test_config, bucket_mgr):
    await bucket_mgr.create(
        content="小雨决定周末去杭州参加朋友婚礼，需要提前买高铁票并准备蓝色连衣裙。",
        name="婚礼安排",
        domain=["人际"],
        tags=["婚礼", "杭州"],
    )
    engine = ImportEngine(test_config, bucket_mgr, DummyDehydrator())

    status = await engine._merge_or_create_item(
        {
            "name": "杭州待办",
            "content": "周末小雨要去杭州参加朋友的婚礼，她需要提前订高铁票，也想带上蓝色连衣裙。",
            "domain": ["事务"],
            "tags": ["高铁", "待办"],
            "importance": 6,
        }
    )

    buckets = await bucket_mgr.list_all(include_archive=False)
    assert status == "duplicate"
    assert len(buckets) == 1


@pytest.mark.asyncio
async def test_import_created_bucket_keeps_source_chunk_metadata(test_config, bucket_mgr):
    engine = ImportEngine(test_config, bucket_mgr, DummyDehydrator())
    status = await engine._merge_or_create_item(
        {
            "name": "来源追踪",
            "content": "小雨希望导入出来的每条记忆都能追溯到原始 chunk，方便之后审查。",
            "domain": ["数字"],
            "tags": ["导入", "追溯"],
            "importance": 6,
            "source_chunk_ids": ["abc123:00001"],
            "source_refs": [
                {
                    "type": "import_chunk",
                    "chunk_id": "abc123:00001",
                    "source_file": "chat.md",
                    "source_hash": "abc123",
                    "chunk_index": 1,
                    "chunk_total": 3,
                    "timestamp_start": "2026-06-01T00:00:00",
                    "timestamp_end": "2026-06-01T00:30:00",
                    "turn_count": 12,
                }
            ],
            "import_source_file": "chat.md",
            "import_source_hash": "abc123",
            "import_timestamp_start": "2026-06-01T00:00:00",
            "import_timestamp_end": "2026-06-01T00:30:00",
        }
    )

    buckets = await bucket_mgr.list_all(include_archive=False)
    meta = buckets[0]["metadata"]
    assert status == "created"
    assert meta["source"] == "import"
    assert meta["source_chunk_ids"] == ["abc123:00001"]
    assert meta["source_refs"][0]["chunk_id"] == "abc123:00001"
    assert meta["import_source_file"] == "chat.md"


@pytest.mark.asyncio
async def test_import_default_does_not_auto_merge_high_search_candidate(test_config):
    class FakeBucketManager:
        def __init__(self):
            self.created = []
            self.updated = False

        async def list_all(self, include_archive=False):
            return []

        async def search(self, *args, **kwargs):
            return [
                {
                    "id": "old",
                    "content": "旧记忆内容和新内容不是同一件事。",
                    "metadata": {
                        "name": "旧桶",
                        "domain": ["数字"],
                        "tags": ["导入"],
                        "score": 99,
                    },
                    "score": 99,
                }
            ]

        async def create(self, **kwargs):
            self.created.append(kwargs)
            return "new"

        async def update(self, *args, **kwargs):
            self.updated = True

    mgr = FakeBucketManager()
    engine = ImportEngine(test_config, mgr, DummyDehydrator())
    status = await engine._merge_or_create_item(
        {
            "name": "新桶",
            "content": "这是一条新的导入记忆，不应该因为 search 综合分高就自动合并。",
            "domain": ["数字"],
            "tags": ["导入"],
        }
    )

    assert status == "created"
    assert len(mgr.created) == 1
    assert mgr.updated is False


@pytest.mark.asyncio
async def test_import_auto_merge_requires_high_content_similarity(test_config):
    class FakeBucketManager:
        def __init__(self):
            self.created = []
            self.updated = False

        async def list_all(self, include_archive=False):
            return []

        async def search(self, *args, **kwargs):
            return [
                {
                    "id": "old",
                    "content": "小雨周末去杭州参加朋友婚礼，需要订高铁票。",
                    "metadata": {
                        "name": "杭州婚礼",
                        "domain": ["事务"],
                        "tags": ["杭州"],
                        "import_source_hash": "same-source",
                    },
                    "score": 99,
                }
            ]

        async def create(self, **kwargs):
            self.created.append(kwargs)
            return "new"

        async def update(self, *args, **kwargs):
            self.updated = True

    cfg = {
        **test_config,
        "import": {
            "auto_merge_enabled": True,
            "merge_threshold": 90,
            "merge_min_content_similarity": 92,
            "merge_require_source_match": True,
        },
    }
    mgr = FakeBucketManager()
    engine = ImportEngine(cfg, mgr, DummyDehydrator())
    status = await engine._merge_or_create_item(
        {
            "name": "杭州餐厅",
            "content": "杭州餐厅清单：想试试西湖边的素食餐厅和甜品店。",
            "domain": ["事务"],
            "tags": ["杭州"],
            "import_source_hash": "same-source",
        }
    )

    assert status == "created"
    assert len(mgr.created) == 1
    assert mgr.updated is False


@pytest.mark.asyncio
async def test_import_preserve_raw_still_dedupes(test_config, bucket_mgr):
    engine = ImportEngine(test_config, bucket_mgr, DummyDehydrator())
    item = {
        "name": "暗号",
        "content": "小雨说某个特殊暗号只属于她和 Haven，要保留原话。",
        "domain": ["恋爱"],
        "tags": ["暗号"],
        "importance": 8,
    }

    first_status = await engine._merge_or_create_item(item, preserve_raw=True)
    second_status = await engine._merge_or_create_item({**item, "name": "重复暗号"}, preserve_raw=True)

    buckets = await bucket_mgr.list_all(include_archive=False)
    assert first_status == "raw"
    assert second_status == "duplicate"
    assert len(buckets) == 1


def test_import_dedupes_items_inside_same_extraction_batch(test_config, bucket_mgr):
    engine = ImportEngine(test_config, bucket_mgr, DummyDehydrator())
    items = [
        {"name": "A", "content": "同一条导入记忆。"},
        {"name": "B", "content": "同一条导入记忆。"},
        {"name": "C", "content": "另一条导入记忆。"},
    ]

    deduped = engine._dedupe_extracted_items(items)

    assert [item["name"] for item in deduped] == ["A", "C"]


def test_import_dedupe_ignores_affect_anchor_inside_same_batch(test_config, bucket_mgr):
    engine = ImportEngine(test_config, bucket_mgr, DummyDehydrator())
    items = [
        {"name": "A", "content": "小雨想让导入去重只看正文主体。"},
        {
            "name": "B",
            "content": (
                "小雨想让导入去重只看正文主体。\n\n"
                "### affect_anchor\n\n"
                "> 小雨把旧信放到桌上。\n"
                "> Dbmaj9 -> Ab/C -> Bbm9\n\n"
                "含义：这只是温度。"
            ),
        },
    ]

    deduped = engine._dedupe_extracted_items(items)

    assert [item["name"] for item in deduped] == ["A"]


@pytest.mark.asyncio
async def test_import_existing_bucket_dedupe_ignores_affect_anchor(test_config, bucket_mgr):
    await bucket_mgr.create(
        content=(
            "小雨决定让导入去重忽略和弦块。\n\n"
            "### affect_anchor\n\n"
            "> 小雨把旧信放到桌上。\n"
            "> Dbmaj9 -> Ab/C -> Bbm9\n\n"
            "含义：这只是温度。"
        ),
        name="导入去重",
        domain=["记忆系统"],
    )
    engine = ImportEngine(test_config, bucket_mgr, DummyDehydrator())

    duplicate = await engine._find_duplicate_bucket("小雨决定让导入去重忽略和弦块。")

    assert duplicate is not None
    assert duplicate["metadata"]["name"] == "导入去重"
