"""The source-position oracle must not credit duplicate captions or wrong scope."""
import copy
import hashlib
import json

import pytest

from reports.evaluate_form_semantics import evaluate


def evaluation_fixture(tmp_path):
    text = "Birth\nBirth\nOnly other applicants"
    proof = tmp_path / "source.txt"
    proof.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(proof.read_bytes()).hexdigest()
    root = tmp_path / "run"
    notice = root / "notice-1"
    notice.mkdir(parents=True)
    (root / "run.json").write_text(json.dumps({"state": "complete"}))
    expected, actual = [], []
    for index in range(2):
        position = {"table_fingerprint": "table", "table_occurrence": 0,
                    "row": 0, "column": 0, "cell_paragraph": index}
        condition = "Only other applicants" if index else None
        item = {"key": f"birth-{index}", "labels": ["Birth"], "physical": position,
                "reference": {"sha256": digest, "location": f"p-{index}"},
                "role": "applicant", "field_type": "text", "semantic_kind": None,
                "required": None, "min_length": None, "max_length": None,
                "length_unit": None, "includes_spaces": None,
                "options": [], "section": None, "condition": condition,
                "manual": False, "guidance": []}
        expected.append(item)
        field = {k: item[k] for k in ("role", "field_type", "semantic_kind", "required",
                                     "min_length", "max_length", "length_unit", "includes_spaces", "options")}
        field.update(id=f"field-{index}", label="Birth", layout=position.copy(), evidence="Birth",
                     source={"sha256": digest, "text_start": index * 6, "text_end": index * 6 + 5})
        if condition:
            field["applicability"] = {"evidence": condition, "source": {
                "sha256": digest, "text_start": 12, "text_end": len(text), "evidence": condition}}
        actual.append(field)
    gold = {"reviewer": "fixture", "limits": [], "forms": [{"id": 1,
            "proof_path": str(proof), "proof_sha256": digest,
            "source_sha_prefixes": [digest], "fields": expected}]}
    prediction = {"eligibility": {"application_fields": actual},
                  "documents": [{"sha256": digest, "extracted_text": text}]}
    return root, notice / "prediction.json", gold, prediction


@pytest.mark.parametrize("corruption", [None, "missing", "duplicate_position", "wrong_type", "swapped_condition", "wrong_proof"])
def test_semantics_oracle_checks_physical_position_and_all_attributes(tmp_path, corruption):
    root, path, gold, prediction = evaluation_fixture(tmp_path)
    fields = prediction["eligibility"]["application_fields"]
    if corruption == "missing":
        fields.pop()
    elif corruption == "duplicate_position":
        fields[1]["layout"] = fields[0]["layout"].copy()
    elif corruption == "wrong_type":
        fields[1]["field_type"] = "image"
    elif corruption == "swapped_condition":
        fields[0]["applicability"] = fields[1].pop("applicability")
    elif corruption == "wrong_proof":
        fields[1]["source"]["text_start"] = 0
        fields[1]["source"]["text_end"] = 4
    path.write_text(json.dumps(prediction), encoding="utf-8")
    result = evaluate(root, copy.deepcopy(gold))
    assert result["fields"]["total"] == 2
    if corruption is None:
        assert result["fields"]["correct"] == 2
        assert result["whole_reviewed_forms"]["correct"] == 1
    else:
        assert result["fields"]["correct"] < 2
        assert result["whole_reviewed_forms"]["correct"] == 0


def test_semantics_oracle_rejects_modified_reviewed_source(tmp_path):
    root, path, gold, prediction = evaluation_fixture(tmp_path)
    path.write_text(json.dumps(prediction), encoding="utf-8")
    with open(gold["forms"][0]["proof_path"], "a", encoding="utf-8") as stream:
        stream.write("changed")
    with pytest.raises(ValueError, match="source checksum mismatch"):
        evaluate(root, gold)


@pytest.mark.parametrize("changed_text", [False, True])
def test_missing_prediction_digest_needs_checksum_verified_matching_capture(tmp_path, changed_text):
    root, path, gold, prediction = evaluation_fixture(tmp_path)
    document = prediction["documents"][0]
    saved = document.copy()
    source = path.parent / "source.txt"
    source.write_text(document["extracted_text"], encoding="utf-8")
    saved["local_file"] = "source.txt"
    (path.parent / "capture.json").write_text(json.dumps({"documents": [saved]}), encoding="utf-8")
    document.pop("sha256")
    if changed_text:
        document["extracted_text"] += "changed"
    path.write_text(json.dumps(prediction), encoding="utf-8")
    result = evaluate(root, gold)
    assert result["fields"]["correct"] == (0 if changed_text else 2)
