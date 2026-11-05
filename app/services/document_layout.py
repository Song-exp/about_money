"""원문 표 구역과 반복 항목의 역할·문자 위치를 보존한다."""
import re
import hashlib
import json


def compact(text):
    return "".join(c for c in text if not c.isspace() and ord(c) >= 32)


def heading_role(text):
    text = re.sub(r"^[\d.()①②③④⑤]+", "", compact(text))
    if len(text) > 90:
        return None
    for prefix, role in (("보호자", "guardian"), ("신청자인적사항", "applicant"),
                         ("신청인인적사항", "applicant"), ("추천인", "recommender"),
                         ("지급대상자", "student"), ("지원대상자", "student"),
                         ("대상자인적사항", "student"), ("학생인적사항", "student")):
        if text.startswith(prefix):
            return role
    return {"학생": "student", "본인": "student", "신청자": "applicant", "신청인": "applicant"}.get(text)


def explicit_role(label):
    value = compact(label)
    if value.startswith(("성명(신청인)", "신청자(", "신청인:", "신청인(")) or value == "신청인":
        return "applicant"
    if value.startswith(("성명(지원대상자본인)", "성명(학생)")):
        return "student"
    if value.startswith("성명(보호자)"):
        return "guardian"
    return None


def finalize_blocks(blocks):
    """셀 병합의 유효 구역 안에서만 역할을 전파하고 원문 offset을 부여한다."""
    headings = []
    cells = {}
    for block in blocks:
        if block.get("cell_location"):
            cells.setdefault(block["cell_location"], []).append(block)
    combined = {}
    for cell_blocks in cells.values():
        text = "\n".join(b["text"] for b in cell_blocks)
        role = heading_role(text)
        if role:
            header = {**cell_blocks[0], "text": text}
            headings.append((header, role))
            combined.update({id(b): role for b in cell_blocks})
    for block in blocks:
        if id(block) in combined:
            continue
        role = heading_role(block["text"])
        if role:
            headings.append((block, role))
    for block in blocks:
        table = block.get("table")
        row, col = block.get("row"), block.get("column")
        candidates = []
        if table and row is not None and col is not None:
            for header, role in headings:
                if header.get("table") != table:
                    continue
                hr, hc = header.get("row"), header.get("column")
                if hr is None or hc is None or hr > row or hc > col:
                    continue
                span = header.get("row_span", 1)
                # 세로 병합 제목은 해당 행들에만 적용한다. 가로 구역 제목은
                # 다음 구역 제목까지 같은 표 안에서 적용한다.
                if span > 1 and row >= hr + span:
                    continue
                if span == 1 and header.get("column_span", 1) <= 1 and hr != row:
                    continue
                candidates.append((hr, hc, header, role))
        if candidates:
            _, _, header, role = max(candidates, key=lambda c: (c[0], c[1]))
            block.update(role=role, role_evidence=header["text"], role_location=header["location"])
        else:
            block.setdefault("role", "unknown")
    # 서명 바로 다음 같은 셀의 생년월일은 그 서명 주체에 속한다.
    # 다른 문단·구역을 넘어 역할을 전파하지 않는다.
    previous = None
    for block in blocks:
        role = explicit_role(block["text"])
        if role:
            block.update(role=role, role_evidence=block["text"], role_location=block["location"])
        elif previous and block.get("cell_location") and block.get("cell_location") == previous.get("cell_location"):
            if compact(block["text"]).startswith("(생년월일:") and explicit_role(previous["text"]):
                block.update(role=previous["role"], role_evidence=previous["text"], role_location=previous["location"])
        previous = block
    position = 0
    for block in blocks:
        block["start"] = position
        position += len(block["text"])
        block["end"] = position
        position += 1
    tables = {}
    for block in blocks:
        if block.get("table"):
            tables.setdefault(block["table"], []).append(block)
    occurrences = {}
    for table_blocks in tables.values():
        signature = [{k: b.get(k) for k in ("row", "column", "row_span", "column_span")}
                     | {"text": compact(b["text"])} for b in table_blocks]
        for block in table_blocks:
            if block.get("table_structure"):
                shape = block["table_structure"]
                signature.append({"native_shape": {"rows": shape.get("rows"), "columns": shape.get("columns"),
                    "cells": [{k: c.get(k) for k in ("row", "column", "row_span", "column_span", "blank", "has_control")}
                              for c in shape["cells"]], "issues": shape.get("issues", [])}})
        fingerprint = hashlib.sha256(json.dumps(signature, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        occurrence = occurrences.get(fingerprint, 0)
        occurrences[fingerprint] = occurrence + 1
        for block in table_blocks:
            block["table_fingerprint"] = fingerprint
            block["table_occurrence"] = occurrence
    return blocks
