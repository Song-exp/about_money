import struct

import pytest
from app.services import hwp_control_context as context
from tests.test_hwp_source_inventory import record, mock_ole


def marker(code):
    return struct.pack("<H", code) + b"klc%" + b"\0" * 8 + struct.pack("<H", code)


def command(text="Clickhere:set:47:Direction:wstring:5:수혜시작일 HelpState:wstring:0: "):
    payload = text.encode("utf-16-le")
    return b"klc%" + b"\1\0\0\0" + b"\x09" + struct.pack("<H", len(payload) // 2) + payload + b"tail"


def test_balanced_and_nested_spans_preserve_visible_defaults_without_using_as_answers():
    payload = marker(3) + "안내".encode("utf-16-le") + marker(3) + "입력 예시".encode("utf-16-le") + marker(4) + marker(4)
    result = context.field_spans(payload)
    assert not result["issues"] and len(result["spans"]) == 2
    assert result["spans"][0]["visible_text"] == "안내입력 예시"
    assert result["spans"][1]["visible_text"] == "입력 예시" and result["spans"][1]["nested"]


@pytest.mark.parametrize("payload,issue", [(marker(3), "field_start_without_end"), (marker(4), "field_end_without_start"),
                                         (marker(3)[:-2] + b"\0\0" + marker(4), "inline_control_boundary_mismatch")])
def test_field_boundary_errors_remain_review_issues(payload, issue):
    assert issue in context.field_spans(payload)["issues"]


@pytest.mark.parametrize("payload", [b"x", b"\3\0", marker(3) + b"\0\xd8" + marker(4)])
def test_truncated_or_invalid_utf16_is_not_dropped(payload):
    with pytest.raises((ValueError, UnicodeDecodeError)):
        context.field_spans(payload)


def test_observed_command_keeps_unused_bytes_and_does_not_execute_instructions():
    observed = context.observed_click_command(command())
    assert "수혜시작일" in observed["text"] and observed["trailing_bytes"] == 4
    assert observed["profile"].startswith("observed_")


@pytest.mark.parametrize("payload", [b"klc%", b"unknown", command("not a click command"), command()[:30],
                                    command()[:8] + b"\x08" + command()[9:]])
def test_unknown_command_profile_is_not_guessed(payload):
    assert context.observed_click_command(payload) is None


def test_control_paragraph_context_uses_header_hierarchy_not_nearest_visible_text():
    data = b"".join([record(66, 0, b"header"), record(67, 1, (marker(3) + "원문 예시".encode("utf-16-le") + marker(4))),
                     record(71, 1, command()), record(66, 2, b"nested"), record(67, 3, "다른 하위 문단".encode("utf-16-le")),
                     record(71, 1, b"daeh"), record(66, 0, b"next"), record(67, 1, "다음 문단".encode("utf-16-le"))])
    result = context.section_context(data, "BodyText/Section0")
    assert len(result["controls"]) == result["unresolved"] == 2
    for item in result["controls"]:
        assert item["paragraph_context"]["record"] == 1 and item["paragraph_context"]["text"] == "원문 예시"
        assert not item["automatic_inheritance"] and not item["target_field_ids"]
        assert not item["paragraph_context"]["mapping_by_order_authorized"]


def test_control_count_mismatch_is_not_authority_to_map_fields():
    data = record(66, 0, b"header") + record(67, 1, "작성 위치 미확정".encode("utf-16-le")) + record(71, 1, command())
    item = context.section_context(data, "BodyText/Section0")["controls"][0]
    assert not item["paragraph_context"]["field_count_matches"] and item["status"] == "requires_original_review"


def test_duplicate_paragraph_text_is_not_arbitrarily_selected():
    data = record(66, 0, b"header") + record(67, 1, b"a\0") + record(67, 1, b"b\0") + record(71, 1, command())
    assert context.section_context(data, "BodyText/Section0")["controls"][0]["paragraph_context"] is None


def test_table_control_and_missing_paragraph_are_not_fake_user_fields():
    result = context.section_context(record(71, 1, b" lbt") + record(71, 1, b"daeh"), "BodyText/Section0")
    assert len(result["controls"]) == 1 and result["controls"][0]["paragraph_context"] is None


@pytest.mark.parametrize("props,body,error", [(2, True, "encrypted_or_distribution_source"), (4, True, "encrypted_or_distribution_source"),
                                          (0, False, "source_body_sections_missing")])
def test_encrypted_preview_only_and_missing_body_fail_closed(monkeypatch, props, body, error):
    mock_ole(monkeypatch, props=props, body=body)
    with pytest.raises(ValueError, match=error):
        context.control_context(b"original")


def test_container_size_limit_precedes_ole_open(monkeypatch):
    monkeypatch.setattr(context, "LIMIT", 4)
    with pytest.raises(ValueError, match="source_container_size_limit"):
        context.control_context(b"large source")
