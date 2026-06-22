import json

import pytest

from raw_events import RawEventStore


class DummyRequest:
    def __init__(self, body=None, query_params=None):
        self._body = body
        self.query_params = query_params or {}
        self.headers = {}
        self.cookies = {}

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _config(tmp_path):
    return {
        "buckets_dir": str(tmp_path / "buckets"),
        "state_dir": str(tmp_path / "state"),
    }


def test_raw_event_store_keeps_only_user_assistant_originals(tmp_path):
    store = RawEventStore(_config(tmp_path))

    result = store.ingest(
        [
            {
                "source_event_id": "u1",
                "role": "user",
                "text": "小雨说这句原话要永远保留。",
                "created_at": "2026-06-22T10:00:00+08:00",
                "conversation_id": "c1",
            },
            {
                "source_event_id": "tool1",
                "role": "tool",
                "text": "tool result should not be archived",
            },
            {
                "source_event_id": "system1",
                "role": "system",
                "text": "system prompt should not be archived",
            },
            {
                "source_event_id": "inj1",
                "role": "assistant",
                "text": "Live private context for the current turn. Use it quietly when relevant.",
            },
        ],
        source="script",
    )

    assert result["inserted"] == 1
    assert result["rejected"] == 3
    assert {item["reason"] for item in result["items"] if item["status"] == "rejected"} == {
        "invalid_role",
        "injected_context",
    }

    duplicate = store.ingest(
        [
            {
                "source_event_id": "u1",
                "role": "user",
                "text": "小雨说这句原话要永远保留。",
                "created_at": "2026-06-22T10:00:00+08:00",
                "conversation_id": "c1",
            }
        ],
        source="script",
    )
    assert duplicate["duplicate"] == 1

    search = store.search("原话", source="script")
    assert search["count"] == 1
    assert search["items"][0]["role"] == "user"
    assert search["items"][0]["text"] == "小雨说这句原话要永远保留。"


def test_raw_event_store_strips_client_context_attachments(tmp_path):
    store = RawEventStore(_config(tmp_path))

    result = store.ingest(
        [
            {
                "source_event_id": "u-auto-context",
                "role": "user",
                "text": (
                    "这句是用户原文 "
                    "<attachment id=\"message_insert_extra_bundle_1\" filename=\"Time:11:07\" "
                    "type=\"text/plain\">【当前时间】 2026-06-22 11:07:21</attachment>"
                    " 结尾"
                ),
            }
        ],
        source="gateway",
    )

    assert result["inserted"] == 1
    search = store.search("用户原文", source="gateway")
    assert search["count"] == 1
    assert search["items"][0]["text"] == "这句是用户原文 结尾"
    assert "attachment" not in search["items"][0]["text"]
    assert "当前时间" not in search["items"][0]["text"]


@pytest.mark.asyncio
async def test_raw_event_http_ingest_and_search(monkeypatch, tmp_path):
    import server

    store = RawEventStore(_config(tmp_path))
    monkeypatch.setattr(server, "raw_event_store", store)
    monkeypatch.setattr(server, "_require_dashboard_auth", lambda request: None)

    ingest_response = await server.api_ingest_raw(
        DummyRequest(
            {
                "source": "script",
                "events": [
                    {
                        "source_event_id": "a1",
                        "role": "assistant",
                        "text": "我把暗号收进原文保险箱。",
                        "created_at": "2026-06-22T11:00:00+08:00",
                    },
                    {
                        "source_event_id": "bad",
                        "role": "assistant",
                        "text": "Recalled Memory\n- [bucket_id:x] 不该收进原文表",
                    },
                ],
            }
        )
    )
    ingest_payload = json.loads(ingest_response.body)

    assert ingest_response.status_code == 200
    assert ingest_payload["inserted"] == 1
    assert ingest_payload["rejected"] == 1

    search_response = await server.api_search_raw(DummyRequest({"q": "暗号", "source": "script"}))
    search_payload = json.loads(search_response.body)

    assert search_response.status_code == 200
    assert search_payload["count"] == 1
    assert search_payload["items"][0]["text"] == "我把暗号收进原文保险箱。"
