"""Edge-path validation coverage for :mod:`omnigent.host.frames`."""

from __future__ import annotations

import pytest

from omnigent.host.frames import decode_host_frame


def test_decode_rejects_non_object_json_payload() -> None:
    with pytest.raises(ValueError, match="frame must be a JSON object"):
        decode_host_frame("[1, 2, 3]")


def test_hello_rejects_non_int_frame_protocol_version() -> None:
    bad = (
        '{"kind": "host.hello", "version": "0.1.0", '
        '"frame_protocol_version": "1", "name": "laptop"}'
    )
    with pytest.raises(ValueError, match="frame_protocol_version"):
        decode_host_frame(bad)


def test_hello_rejects_non_string_runner_tokens() -> None:
    bad = (
        '{"kind": "host.hello", "version": "0.1.0", '
        '"frame_protocol_version": 1, "name": "laptop", "runners": [1, 2]}'
    )
    with pytest.raises(ValueError, match="runners"):
        decode_host_frame(bad)


def test_list_dir_rejects_non_int_limit() -> None:
    bad = (
        '{"kind": "host.list_dir", "request_id": "r", "path": "/tmp", '
        '"limit": "twenty"}'
    )
    with pytest.raises(ValueError, match="limit"):
        decode_host_frame(bad)


def test_list_dir_result_rejects_non_list_entries() -> None:
    bad = (
        '{"kind": "host.list_dir_result", "request_id": "r", "status": "ok", '
        '"entries": "bad", "has_more": false}'
    )
    with pytest.raises(ValueError, match="entries"):
        decode_host_frame(bad)


def test_list_dir_result_rejects_non_object_entry() -> None:
    bad = (
        '{"kind": "host.list_dir_result", "request_id": "r", "status": "ok", '
        '"entries": [1], "has_more": false}'
    )
    with pytest.raises(ValueError, match="each entry"):
        decode_host_frame(bad)


def test_list_dir_result_rejects_non_bool_has_more() -> None:
    bad = (
        '{"kind": "host.list_dir_result", "request_id": "r", "status": "ok", '
        '"entries": [], "has_more": "yes"}'
    )
    with pytest.raises(ValueError, match="has_more"):
        decode_host_frame(bad)


def test_list_dir_result_rejects_non_int_entry_bytes() -> None:
    bad = (
        '{"kind": "host.list_dir_result", "request_id": "r", "status": "ok", '
        '"entries": [{"name": "x", "path": "/x", "type": "file", '
        '"bytes": "big", "modified_at": 1}], "has_more": false}'
    )
    with pytest.raises(ValueError, match="bytes"):
        decode_host_frame(bad)


def test_list_dir_result_rejects_non_string_error_field() -> None:
    bad = (
        '{"kind": "host.list_dir_result", "request_id": "r", "status": "failed", '
        '"entries": [], "has_more": false, "error": 500}'
    )
    with pytest.raises(ValueError, match="error"):
        decode_host_frame(bad)