from __future__ import annotations

import json
import logging

from app.observability import JsonFormatter

API = "/api/v1"


def test_json_formatter_emits_structured_fields():
    record = logging.LogRecord(
        "mindbridge.engine3.api",
        logging.INFO,
        __file__,
        1,
        "scene.updated",
        None,
        None,
    )
    record.request_id = "trace-1"
    record.scene_id = "bus-tracking-demo"
    record.revision = 3
    record.warning_count = 1

    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "mindbridge.engine3.api"
    assert payload["event"] == "scene.updated"
    assert payload["request_id"] == "trace-1"
    assert payload["scene_id"] == "bus-tracking-demo"
    assert payload["revision"] == 3
    assert payload["warning_count"] == 1


def test_json_formatter_includes_exception_text():
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            "mindbridge.engine3",
            logging.ERROR,
            __file__,
            1,
            "http.unhandled",
            None,
            sys.exc_info(),
        )

    payload = json.loads(JsonFormatter().format(record))
    assert "RuntimeError: boom" in payload["exception"]


def test_scene_operations_are_logged_without_sensitive_content(
    client, sample_instruction, caplog
):
    with caplog.at_level(logging.INFO, logger="mindbridge.engine3.api"):
        client.post(f"{API}/visualize", json=sample_instruction)
        client.get(f"{API}/scenes/bus-tracking-demo")
        client.post(f"{API}/scenes/bus-tracking-demo/update", json={"title": "Second title"})

    events = [record for record in caplog.records if record.name == "mindbridge.engine3.api"]
    messages = [record.getMessage() for record in events]
    assert "scene.visualized" in messages
    assert "scene.fetched" in messages
    assert "scene.updated" in messages

    for record in events:
        message = record.getMessage()
        assert "Live Bus Tracking System" not in message
        assert "Second title" not in message
        assert "Bus 101" not in message
        assert getattr(record, "scene_id", None) == "bus-tracking-demo"
        assert getattr(record, "revision", None) is not None


def test_error_requests_are_logged_with_status(client, caplog):
    with caplog.at_level(logging.INFO, logger="mindbridge.engine3"):
        response = client.get(f"{API}/scenes/never-created")

    errors = [
        record
        for record in caplog.records
        if record.name == "mindbridge.engine3" and record.getMessage() == "http.error"
    ]
    assert errors
    assert errors[-1].status == 404
    assert errors[-1].request_id == response.headers["x-request-id"]
