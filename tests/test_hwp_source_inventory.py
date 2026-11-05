import hashlib
import io
import struct
import zlib

import pytest
from app.services import hwp_source_inventory as inventory


def record(tag, level, payload, extended=False):
    size = 0xfff if extended else len(payload)
    return struct.pack("<I", tag | level << 10 | size << 20) + (struct.pack("<I", len(payload)) if extended else b"") + payload


def test_record_reader_accounts_for_full_stream_and_extended_headers():
    data = record(71, 1, b" lbt") + record(67, 2, "내용".encode("utf-16-le"), True)
    result = inventory.record_inventory(data)
    assert result[-1]["end_offset"] == len(data) and result[-1]["table_parent"] == 0
    assert result[-1]["payload_sha256"] == hashlib.sha256("내용".encode("utf-16-le")).hexdigest()


@pytest.mark.parametrize("data", [b"x", struct.pack("<I", 67 | 0xfff << 20), record(67, 1, b"abc")[:-1]])
def test_truncated_records_are_not_reported_as_complete(data):
    with pytest.raises(ValueError):
        inventory.record_inventory(data)


def test_controls_lists_and_unclassified_headers_remain_in_inventory():
    header = bytearray(34)
    struct.pack_into("<HHHH", header, 8, 0, 0, 1, 1)
    data = b"".join([record(71, 1, b"daeh"), record(72, 2, b"short"), record(71, 1, b" lbt"),
                     record(77, 2, b"\0" * 4 + struct.pack("<HH", 1, 1)), record(72, 2, bytes(header)),
                     record(66, 3, struct.pack("<I", 1)), record(71, 1, b"klc%"), record(67, 2, "날짜".encode("utf-16-le"))])
    result = inventory.section_inventory(data, "BodyText/Section0")
    assert len(result["controls"]) == 3 and len(result["list_records"]) == 2
    assert result["list_records"][0]["kind"] == "non_table_list"
    assert result["list_records"][1]["blank_candidate"] is True
    assert result["texts"][0]["table_parent"] is None


def test_nested_object_is_not_a_blank_cell_candidate():
    header = bytearray(34)
    struct.pack_into("<HHHH", header, 8, 0, 0, 1, 1)
    data = record(71, 1, b" lbt") + record(77, 2, b"\0" * 4 + struct.pack("<HH", 1, 1)) + record(72, 2, bytes(header)) + record(66, 3, struct.pack("<I", 1)) + record(71, 3, b"klc%")
    cell = inventory.section_inventory(data, "BodyText/Section0")["list_records"][0]
    assert cell["has_control"] and not cell["blank_candidate"]


def test_independent_text_reader_preserves_unicode_and_skips_control_payload():
    text = "한글🙂".encode("utf-16-le") + struct.pack("<H", 9) + b"x" * 14 + "끝\r".encode("utf-16-le")
    assert inventory.paragraph_text(text) == "한글🙂\t끝"


@pytest.mark.parametrize("data", [b"x", struct.pack("<H", 9), b"\x00\xd8"])
def test_invalid_text_encoding_or_control_is_not_silently_dropped(data):
    with pytest.raises((ValueError, UnicodeDecodeError)):
        inventory.paragraph_text(data)


def test_deflate_trailing_bytes_are_accounted_for_without_interpretation():
    compressor = zlib.compressobj(wbits=-15)
    data = compressor.compress(b"original") + compressor.flush() + b"tail"
    text, accounting = inventory.inflate(data, True)
    assert text == b"original" and accounting["unused_tail_bytes"] == 4
    assert accounting["unused_tail_sha256"] == hashlib.sha256(b"tail").hexdigest() and accounting["tail_review_status"] == "pending"
    with pytest.raises(ValueError):
        inventory.inflate(data[:3], True)


def test_decompression_and_record_limits_fail_closed(monkeypatch):
    compressor = zlib.compressobj(wbits=-15)
    data = compressor.compress(b"a" * 1000) + compressor.flush()
    monkeypatch.setattr(inventory, "LIMIT", 32)
    with pytest.raises(ValueError):
        inventory.inflate(data, True)
    with pytest.raises(ValueError):
        inventory.record_inventory(b"a" * 33)


def mock_ole(monkeypatch, props=0, body=True):
    header = bytearray(40)
    header[:17] = b"HWP Document File"
    struct.pack_into("<I", header, 36, props)
    streams = {"FileHeader": bytes(header), "Scripts/DefaultJScript": b"do not execute"}
    if body:
        streams["BodyText/Section0"] = record(67, 1, "문항".encode("utf-16-le"))
    class Ole:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def listdir(self):
            return [p.split("/") for p in streams]
        def openstream(self, entry):
            return io.BytesIO(streams[entry if isinstance(entry, str) else "/".join(entry)])
        def get_size(self, entry):
            return len(streams["/".join(entry)])
    monkeypatch.setattr(inventory.olefile, "OleFileIO", lambda *args: Ole())


def test_container_inventory_keeps_all_streams_and_does_not_claim_semantic_gold(monkeypatch):
    mock_ole(monkeypatch)
    result = inventory.source_inventory(b"original-container")
    assert len(result["streams"]) == 3 and result["sections"][0]["texts"][0]["text"] == "문항"
    assert not result["complete_atomic_gold"] and not result["visual_review_complete"] and not result["whole_form_complete"]


@pytest.mark.parametrize("props", [2, 4])
def test_encrypted_or_distribution_source_cannot_be_inventory_success(monkeypatch, props):
    mock_ole(monkeypatch, props)
    with pytest.raises(ValueError, match="encrypted_or_distribution"):
        inventory.source_inventory(b"source")


def test_preview_only_source_cannot_replace_full_body(monkeypatch):
    mock_ole(monkeypatch, body=False)
    with pytest.raises(ValueError, match="sections_missing"):
        inventory.source_inventory(b"source")
