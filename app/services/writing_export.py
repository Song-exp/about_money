"""원본 양식에 옮길 문항별 초안을 TXT/DOCX로 내보낸다. 원본 레이아웃을 채운 파일은 아니다."""
import io
from fastapi import HTTPException
from app.services.writing_workflow import checked_drafts
from app.services.writing_requirements import field_label
from app.services.form_semantics import active_fields, is_manual


def export(db, session, format):
    scholarship, drafts = checked_drafts(db, session)
    lines = [f"{scholarship.title} — 문항별 신청서 초안", f"공고: {scholarship.source_url}", f"양식 버전: {session.form_fingerprint}",
             "원본 양식에 옮겨 작성하고 서명·첨부·제출 방법을 직접 확인하세요.", ""]
    manual = [f for f in active_fields(session.fields, session.workflow or {}) if is_manual(f)]
    groups = (session.workflow or {}).get("form_groups", [])
    group_lines = [f"{g['label']} 전체에 적용: {g['evidence']}\n원본에 배치한 뒤 전체 분량과 서식을 직접 확인하세요." +
                   "".join("\n" + s["evidence"] for s in (g.get("history_reuse_policy") or {}).get("sources", [])) for g in groups]
    def manual_text(field):
        if (field.get("manual_spec") or {}).get("mode") == "original_only":
            action = field["manual_spec"]["action"]
        elif field.get("semantic_kind") == "submission_checklist":
            item = (session.answers or {}).get(field["id"], {}).get("facts", {})
            status = item.get("preparation_status", {}).get("text", "미확인")
            acknowledgement = item.get("rules_ack", {}).get("text", "미확인")
            action = f"사용자 보고 준비 상태: {status}; 원문 조건 확인: {acknowledgement}. 실제 파일 검증·첨부·제출 완료를 뜻하지 않습니다. " + field["manual_spec"]["action"]
        elif field.get("semantic_kind") == "consent":
            selection = (session.answers or {}).get(field["id"], {}).get("text", "")
            action = f"사용자가 선택한 내용: {selection or '선택 항목 생략'}. 동의 내용을 읽고 원본에서 직접 표시"
        elif field["field_type"] == "signature":
            action = "원본에서 직접 서명"
        elif field["field_type"] == "image":
            action = "원문 규격에 맞는 사진·이미지를 직접 준비하여 첨부"
        else:
            action = "원문 지침에 맞는 파일·서류를 직접 준비하여 첨부"
        condition = (field.get("applicability") or {}).get("evidence")
        return f"{field_label(field)}: {action}\n원문 지침: {(field.get('consent_context') or {}).get('text') or field.get('instructions') or field['evidence']}" + (f"\n적용 조건: {condition}" if condition else "")
    if format == "txt":
        if group_lines:
            lines.extend(["문항 묶음의 작성 규칙", *group_lines, ""])
        for draft in drafts:
            lines.extend([draft["label"], draft["text"], ""])
        if manual:
            lines.extend(["원본에서 직접 처리할 항목", *[manual_text(f) for f in manual]])
        return "\n".join(lines).encode("utf-8-sig"), "text/plain; charset=utf-8"
    if format != "docx":
        raise HTTPException(422, "TXT 또는 DOCX 형식을 선택하세요.")
    from docx import Document
    document = Document()
    document.styles["Normal"].font.name = "Malgun Gothic"
    document.add_heading(lines[0], level=0)
    for line in lines[1:4]:
        document.add_paragraph(line)
    for line in group_lines:
        document.add_paragraph(line)
    for draft in drafts:
        document.add_heading(draft["label"], level=1)
        for paragraph in draft["text"].split("\n\n"):
            document.add_paragraph(paragraph)
    for field in manual:
        document.add_paragraph(manual_text(field))
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
