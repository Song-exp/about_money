from copy import deepcopy

import pytest

from reports.form_semantic_reference import CLASSIFICATIONS, SOURCE_SHA256, build_gold, audit_semantic_reference


def fixture():
    descriptions = [(40, "input_label", "성명"), (49, "input_label", "관계"),
                    (145, "example", "예시: √"), (1035, "manual_original", "2026년 월 일")]
    items, blocks, offset = [], [], 0
    for record, kind, text in descriptions:
        items.append({"record": record, "kind": kind, "source": {"record": record, "text": text}})
        blocks.append({"text": text, "location": f"Section0/record:{record}", "start": offset, "end": offset + len(text)})
        offset += len(text) + 1
    gold = {"items": items, "source_sha256": SOURCE_SHA256, "scope": "text_classification"}
    first = {"id": "name", "field_type": "text", "evidence": "성명\n관계", "source": {
        "sha256": SOURCE_SHA256, "location": blocks[0]["location"], "text_start": 0, "text_end": blocks[1]["end"]}}
    date = {"id": "date", "field_type": "text", "manual_spec": {"mode": "original_only"},
        "source": {"sha256": SOURCE_SHA256, "location": blocks[-1]["location"], "evidence": blocks[-1]["text"],
                   "text_start": blocks[-1]["start"], "text_end": blocks[-1]["end"]}}
    return gold, blocks, [first, date]


def test_multi_paragraph_label_with_field_level_evidence_is_original_span():
    gold, blocks, fields = fixture()
    before = deepcopy((gold, blocks, fields))
    audit = audit_semantic_reference(gold, blocks, fields)
    assert audit["issue_count"] == 0
    assert audit["complete_atomic_gold"] is audit["whole_form_complete"] is audit["independent_human_review_complete"] is False
    assert (gold, blocks, fields) == before


@pytest.mark.parametrize("problem", ["missing", "duplicate", "changed", "offset", "unknown_record"])
def test_source_loss_and_corrupted_source_boundaries_are_detected(problem):
    gold, blocks, fields = fixture()
    if problem == "missing":
        blocks.pop(2)
    elif problem == "duplicate":
        blocks.append(deepcopy(blocks[2]))
    elif problem == "changed":
        blocks[2]["text"] = "실제 첨부 완료"
    elif problem == "offset":
        blocks[2]["start"] += 1
    else:
        blocks.append({"text": "새 문구", "location": "Section0/record:9999"})
    assert audit_semantic_reference(gold, blocks, fields)["issue_count"] > 0


@pytest.mark.parametrize("kind", ["example", "instruction", "consent_context", "unresolved_reference", "detail_prompt"])
def test_non_input_source_cannot_become_new_field(kind):
    gold, blocks, fields = fixture()
    gold["items"][2]["kind"] = kind
    fields.append({"id": "false-field", "source": {"sha256": SOURCE_SHA256, "location": blocks[2]["location"],
        "evidence": blocks[2]["text"], "text_start": blocks[2]["start"], "text_end": blocks[2]["end"]}})
    assert "non_input_record_used_as_field" in {i["reason"] for i in audit_semantic_reference(gold, blocks, fields)["issues"]}


@pytest.mark.parametrize("problem", ["foreign_hash", "invented_quote", "wrong_record", "invalid_span", "editable_date"])
def test_wrong_provenance_and_original_date_policy_are_detected(problem):
    gold, blocks, fields = fixture()
    if problem == "foreign_hash":
        fields[0]["source"]["sha256"] = "different-document"
    elif problem == "invented_quote":
        fields[0]["evidence"] += " invented"
    elif problem == "wrong_record":
        fields[0]["source"]["location"] = "Section0/record:9999"
    elif problem == "invalid_span":
        fields[0]["source"]["text_start"] = -1
    else:
        fields[-1].pop("manual_spec")
    assert audit_semantic_reference(gold, blocks, fields)["issue_count"] > 0


def annotation_inventory():
    records = sorted(r for values in CLASSIFICATIONS.values() for r in values)
    return {"source_sha256": SOURCE_SHA256, "sections": [{"texts": [{"record": r, "text": str(r)} for r in records]}]}


def test_annotation_covers_all_preserved_records_and_keeps_stream_order_distinct_from_page_order():
    gold = build_gold(annotation_inventory())
    assert len(gold["items"]) == 239
    assert next(i for i in gold["items"] if i["record"] == 1105)["page"] == 4
    assert sum(gold["category_counts"].values()) == 239
    assert not gold["complete_atomic_gold"] and not gold["independent_human_review_complete"]


@pytest.mark.parametrize("problem", ["missing", "extra", "duplicate", "foreign_document"])
def test_annotation_rejects_unclassified_duplicate_and_different_originals(problem):
    inventory = annotation_inventory()
    texts = inventory["sections"][0]["texts"]
    if problem == "missing":
        texts.pop()
    elif problem == "extra":
        texts.append({"record": 9999, "text": "new"})
    elif problem == "duplicate":
        texts.append(deepcopy(texts[0]))
    else:
        inventory["source_sha256"] = "wrong"
    with pytest.raises(ValueError):
        build_gold(inventory)
