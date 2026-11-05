"""작성 대화: 자격 확인 → 문항 사실 수집·확인 → 근거 초안 → 검토·저장."""
import re
from copy import deepcopy
from types import SimpleNamespace
from fastapi import HTTPException
from sqlalchemy import select, update
from app.models.application import ApplicationDocument, ApplicationResult, ApplicationSource, DocType, UserApplication
from app.models.scholarship import Scholarship
from app.models.writing_session import WritingSession
from app.schemas.schemas import UserProfile
from app.services import chat
from app.services.scholarship_readiness import readiness
from app.services.writing_requirements import normalize_answers, next_action, notice_fingerprint, writing_fingerprint, requirements
from app.services.writing_drafts import compose, validate_length, draft_digest, validate_groups
from app.services.form_content import duplicate_slot
from app.utils.pii import strip_rrn
from app.utils.text import strip_nul
from app.services.form_semantics import MANUAL_TYPES, is_manual, applicability_state, active_fields, original_unchecked
from app.services.document_layout import compact
from app.services.writing_requirements import field_label
from app.services.writing_conversation import interpret as interpret_writing, action_key, next_question


def _clean(text):
    return strip_nul(strip_rrn(text.strip()))


def _affirm(text):
    return bool(re.search(r"^(?:네|예|맞|확인|사실|충족|해당)|모두.*(?:충족|해당)", text)) and not re.search(r"아니|않|(?<!잘)못|모르|미확인|불충족|해당.*없", text)


def _action(session, scholarship, answers, workflow, profile=None):
    proxy = SimpleNamespace(fields=session.fields, profile=profile or session.profile)
    return next_action(proxy, scholarship, answers, workflow)


def view(db, session):
    scholarship = db.get(Scholarship, session.scholarship_id)
    workflow = session.workflow or {}
    from app.services.essay_collection import needs_recollection
    if needs_recollection(session):
        workflow = deepcopy(workflow)
        _invalidate(workflow)
    answers = normalize_answers(session)
    current = readiness(scholarship)
    from app.services.consultation_scope import in_scope
    if not in_scope(scholarship, 'scholarship'):
        current = {**current, 'writing_ready': False, 'reasons': [*current['reasons'], 'policy_information_only']}
    stale = (writing_fingerprint(scholarship) != session.form_fingerprint or
             workflow.get("notice_fingerprint", notice_fingerprint(scholarship)) != notice_fingerprint(scholarship))
    action = _action(session, scholarship, answers, workflow)
    drafts = workflow.get("drafts", [])
    group_validations = validate_groups(workflow.get("form_groups", []), session.fields, drafts, workflow)
    if stale:
        state = "stale"
    elif not current["writing_ready"]:
        state = "blocked"
    elif action:
        state = "ineligible" if action.get("failed") or workflow.get("additional_qualification_failed") else "awaiting_confirmation" if action["kind"] == "confirm" else "collecting"
    elif drafts:
        state = "needs_revision" if any(d["validation"]["errors"] for d in drafts) or any(g["errors"] for g in group_validations) else "saved" if workflow.get("saved_digest") == draft_digest(drafts) and session.application_id else "review"
    else:
        state = "ready"
    messages = list(session.messages)
    if action and state not in ("blocked", "stale") and workflow.get('last_reply_action') != action_key(action) and (not messages or messages[-1].get("text") != action["question"]):
        messages.append({"role": "bot", "text": action["question"]})
    current_field = next((f for f in session.fields if action and f["id"] == action.get("field_id")), None)
    plans = {f["id"]: requirements(f, answers.get(f["id"])) for f in session.fields}
    conditions = {}
    for field in session.fields:
        if field.get("applicability"):
            condition = field["applicability"]
            conditions.setdefault(condition["id"], {**condition, "state": applicability_state(field, workflow)})
    manual_items = [{"field_id": f["id"], "label": field_label(f), "field_type": f["field_type"],
                     "instructions": (f.get("consent_context") or {}).get("text") or f.get("instructions") or f["evidence"], "options": f.get("options", []),
                     "selection": answers.get(f["id"], {}).get("text", "") if f.get("semantic_kind") in ("consent", "submission_checklist") else "",
                     "semantic_kind": f.get("semantic_kind"), "source": f.get("source"),
                     "action": (f.get("manual_spec") or {}).get("action"),
                     "condition_evidence": (f.get("applicability") or {}).get("evidence")}
                    for f in active_fields(session.fields, workflow) if is_manual(f)]
    return {"id": str(session.id), "scholarship_id": scholarship.id, "title": scholarship.title,
            "source_url": scholarship.source_url, "revision_id": session.revision_id, "form_fingerprint": session.form_fingerprint,
            "state": state, "version": session.version, "readiness": current, "profile": session.profile,
            "fields": session.fields, "requirements": plans, "messages": messages, "answers": answers,
            "current_field": current_field, "next_action": action if state not in ("blocked", "stale") else None,
            "conditions": list(conditions.values()), "manual_items": manual_items,
            "form_groups": workflow.get("form_groups", []), "group_validations": group_validations,
            "applicability_states": {f["id"]: applicability_state(f, workflow) for f in session.fields},
            "drafts": drafts, "draft_reviewed": bool(drafts) and workflow.get("reviewed_digest") == draft_digest(drafts),
            "application_id": str(session.application_id) if session.application_id else None}


def _active(db, session):
    scholarship = db.get(Scholarship, session.scholarship_id)
    db.refresh(scholarship)
    if view(db, session)["state"] in ("blocked", "stale"):
        raise HTTPException(409, "공고 또는 양식 상태가 변경되었습니다. 최신 공고를 확인하세요.")
    return scholarship


def _commit(db, session, expected_version, answers, workflow, messages, **extra):
    scholarship = _active(db, session)
    workflow["notice_fingerprint"] = notice_fingerprint(scholarship)
    changed = db.execute(update(WritingSession).where(WritingSession.id == session.id, WritingSession.version == expected_version)
                         .values(answers=answers, workflow=workflow, messages=messages, version=expected_version + 1, **extra))
    if changed.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "대화가 변경되었습니다. 최신 대화를 다시 불러오세요.")
    db.commit()
    db.refresh(session)
    return view(db, session)


def _invalidate(workflow):
    for key in ("drafts", "reviewed_digest", "saved_digest", "manual_checks_confirmed", "last_reply_action"):
        workflow.pop(key, None)


def _field(session, field_id):
    field = next((f for f in session.fields if f["id"] == field_id and f["field_type"] not in MANUAL_TYPES and (f.get("manual_spec") or {}).get("mode") != "original_only"), None)
    if field is None:
        raise HTTPException(422, "실제 양식의 작성 항목을 선택하세요.")
    if applicability_state(field, session.workflow or {}) != "applicable":
        raise HTTPException(409, "이 문항의 작성 조건을 먼저 확인하세요.")
    return field


def _check_choice(field, text, slot_key="value", answer=None):
    from app.services.essay_collection import check_detail
    try:
        check_detail(field, slot_key, text, answer)
    except ValueError:
        raise HTTPException(422, "지원 여부를 명시된 응답으로 확인하고 해당하는 세부 내용만 입력하세요.") from None
    if (field.get("manual_spec") or {}).get("mode") == "original_only":
        raise HTTPException(422, "이 항목은 채팅 대신 원본에서 직접 작성하세요.")
    if field.get("input_spec"):
        from app.services.form_profile_inputs import check_input
        try:
            check_input(field, slot_key, text, answer)
        except ValueError:
            raise HTTPException(422, "원문에서 요청한 세부 정보를 정확하게 입력하세요.") from None
        return
    if field.get("choice_spec"):
        from app.services.form_choice_values import check_slot
        try:
            check_slot(field, slot_key, text, answer)
        except ValueError:
            raise HTTPException(422, "원문 선택지와 선택한 항목의 값·단위를 확인하세요.") from None
        return
    if field["field_type"] == "checkbox" and field.get("options") and compact(text) not in {compact(o) for o in field["options"]} and not original_unchecked(field, text):
        raise HTTPException(422, "원문에 있는 선택지 중 하나를 그대로 선택하세요.")


def set_applicability(db, session, condition_id, applies, expected_version, messages=None):
    _active(db, session)
    affected = [f for f in session.fields if (f.get("applicability") or {}).get("id") == condition_id]
    if not affected or type(applies) is not bool:
        raise HTTPException(422, "원문의 작성 조건과 해당 여부를 확인하세요.")
    answers, workflow = normalize_answers(session), deepcopy(session.workflow or {})
    previous = workflow.get("applicability_answers", {}).get(condition_id, {})
    changed = previous.get("confirmed") is not True or previous.get("applies") is not applies
    workflow.setdefault("applicability_answers", {})[condition_id] = {
        "applies": applies, "confirmed": True, "source": {"kind": "user_confirmation"}}
    for field in affected:
        if not changed:
            continue
        item = answers.get(field["id"])
        if item:
            if field["applicability"].get("discard_facts_on_change"):
                answers.pop(field["id"], None)
                continue
            for fact in item.get("facts", {}).values():
                fact["confirmed"] = False
            if item.get("facts"):
                item["status"] = "provided_unconfirmed"
    if changed:
        _invalidate(workflow)
    following = _action(session, db.get(Scholarship, session.scholarship_id), answers, workflow)
    messages = messages or list(session.messages)
    messages.append({"role": "bot", "text": ("해당 조건의 문항을 작성 대상으로 확인했습니다." if applies else "해당하지 않는 문항은 이번 초안과 준비 항목에서 제외합니다.")})
    if following:
        messages.append({"role": "bot", "text": following["question"]})
    return _commit(db, session, expected_version, answers, workflow, messages)


def _set_fact(answers, field, slot_key, text, source, confirmed=False):
    if (field.get("manual_spec") or {}).get("mode") == "original_only":
        raise HTTPException(422, "이 항목은 채팅 대신 원본에서 직접 작성하세요.")
    from app.services.essay_responses import response_issue, followup
    issue = response_issue(field, slot_key, text)
    if issue:
        raise HTTPException(422, followup(issue))
    item = answers.setdefault(field["id"], {"label": field["label"], "facts": {}})
    from app.services.essay_collection import clear_dependents
    _check_choice(field, text, slot_key, item)
    clear_dependents(field, item, slot_key, text)
    from app.services.form_spreadsheet_education import clear_education_dependents
    clear_education_dependents(field, item, slot_key)
    if field.get("input_spec"):
        _check_choice(field, text, slot_key, item)
        if field["input_spec"].get("preparation_only") and slot_key == "preparation_status":
            item.get("facts", {}).pop("rules_ack", None)
        repeat = field["input_spec"].get("repeat")
        if repeat and slot_key == repeat["count_key"]:
            item["facts"] = {k: v for k, v in item.get("facts", {}).items() if not k.startswith("entry_")}
        for fact in item.get("facts", {}).values():
            fact["confirmed"] = False
    if field.get("choice_spec"):
        _check_choice(field, text, slot_key, item)
        if slot_key == "value":
            from app.services.form_choice_values import selections
            selected = selections(field, text)
            text = "\n".join(selected) if selected else "표시하지 않음"
            item["facts"] = {}
        for fact in item.get("facts", {}).values():
            fact["confirmed"] = False
    item.setdefault("facts", {})[slot_key] = {"text": text, "confirmed": confirmed, "source": source}
    if field.get("selection_mode") == "single_mark":
        item["facts"][slot_key]["selection_state"] = "unchecked" if original_unchecked(field, text) else "checked"
    item.update(text="\n\n".join(dict.fromkeys(f["text"] for f in item["facts"].values())), status="provided_unconfirmed")


def skip_field(db, session, field_id, expected_version):
    _active(db, session)
    field = _field(session, field_id)
    if field.get("required") is not False:
        raise HTTPException(422, "원문에서 선택 항목으로 확인된 문항만 생략할 수 있습니다.")
    answers, workflow = normalize_answers(session), deepcopy(session.workflow or {})
    answers[field_id] = {"label": field["label"], "text": "", "facts": {}, "status": "skipped"}
    _invalidate(workflow)
    return _commit(db, session, expected_version, answers, workflow, list(session.messages))


def message(db, session, text, expected_version):
    scholarship = _active(db, session)
    current = view(db, session)
    action = current["next_action"]
    text = _clean(text)
    if not text:
        raise HTTPException(422, "정확한 본인 정보를 입력하세요.")
    answers, workflow, profile = current["answers"], deepcopy(session.workflow or {}), dict(session.profile)
    messages = [*current["messages"], {"role": "user", "text": text}]
    turn = interpret_writing(session, current, text)
    if turn.kind in ('question', 'unknown'):
        workflow['last_reply_action'] = action_key(action)
        messages.append({'role': 'bot', 'text': turn.reply})
        return _commit(db, session, expected_version, answers, workflow, messages)
    if turn.kind == 'correction':
        field = _field(session, turn.field_id)
        _check_choice(field, turn.fact_quote, turn.slot_key, answers.get(field['id']))
        from app.services.essay_responses import response_issue, followup
        issue = response_issue(field, turn.slot_key, turn.fact_quote)
        if issue:
            raise HTTPException(422, followup(issue))
        _set_fact(answers, field, turn.slot_key, turn.fact_quote, {'kind': 'conversation_revision'})
        _invalidate(workflow)
        following = _action(session, scholarship, answers, workflow)
        messages.append({'role': 'bot', 'text': next_question(session, following, messages)})
        workflow['last_reply_action'] = action_key(following)
        return _commit(db, session, expected_version, answers, workflow, messages)
    if turn.kind == 'rewrite':
        return rewrite_draft(db, session, turn.field_id, text, expected_version, messages)
    if action is None:
        raise HTTPException(409, "현재 수집할 항목이 없습니다. 초안을 생성하거나 항목별 내용을 수정하세요.")
    if turn.kind == 'fact' and turn.fact_quote:
        text = turn.fact_quote
    if action["kind"] == "applicability":
        if _affirm(text):
            return set_applicability(db, session, action["condition_id"], True, expected_version, messages)
        if re.fullmatch(r"아니(?:요|오)?[.! ]*|해당하지\s*않습니다[.! ]*", text):
            return set_applicability(db, session, action["condition_id"], False, expected_version, messages)
        messages.append({"role": "bot", "text": "아직 작성 조건을 확정하지 않았습니다. 해당하면 '예', 해당하지 않으면 '아니요'로 알려주세요."})
        return _commit(db, session, expected_version, answers, workflow, messages)
    if action["kind"] == "qualification":
        if action['key'].startswith('rule:'):
            node = action['rule']
            if node['attribute'] == 'reported':
                if text.strip() not in ('예', '아니요'):
                    raise HTTPException(422, '해당 여부를 예 또는 아니요로 알려주세요.')
                workflow.setdefault('qualification_answers', {})[node['key']] = text.strip() == '예'
            else:
                attribute = node['attribute']
                value = text.strip()
                if attribute in ('gpa', 'gpa_scale'):
                    try:
                        value = float(value)
                    except ValueError:
                        raise HTTPException(422, '실제 학점 또는 만점 기준을 숫자로 알려주세요.')
                elif attribute == 'income_bracket':
                    if not re.fullmatch('[0-9]+', value):
                        raise HTTPException(422, '소득구간을 숫자로 알려주세요.')
                    value = int(value)
                profile[attribute] = value
                try:
                    profile = UserProfile.model_validate(profile).model_dump()
                except ValueError:
                    raise HTTPException(422, '조건 값의 범위를 확인하세요.')
        elif action['key'] == 'rules_review':
            raise HTTPException(409, '검토된 조건이 충족되지 않았거나 원문 재검토가 필요합니다.')
        elif action["key"] == "additional":
            workflow["additional_qualification_confirmed"] = _affirm(text)
            workflow["additional_qualification_failed"] = bool(re.search("아니|불충족|해당.*없|조건.*못", text))
            workflow["qualification_evidence"] = text
        else:
            key = action["key"]
            model = UserProfile.model_validate(profile)
            chat._parse_profile(text, model)
            bare = text.strip().rstrip("점")
            if key == "gpa" and re.fullmatch(r"\d+(?:\.\d+)?", bare):
                model.gpa = float(bare)
            elif key in ("income_bracket", "grade_level") and re.fullmatch(r"\d+", bare):
                setattr(model, key, int(bare) if key == "income_bracket" else bare)
            if key == "region" and len(text) < 30 and not re.search("모르|미확인", text):
                model.region = text
            try:
                profile = UserProfile.model_validate(model.model_dump()).model_dump()
            except ValueError:
                raise HTTPException(422, "조건 값의 범위를 확인하세요.")
    elif action["kind"] == "confirm":
        if not _affirm(text):
            messages.append({"role": "bot", "text": "항목별 수정 버튼으로 내용을 정정하거나, 사실과 맞으면 확인 버튼을 눌러주세요."})
            return _commit(db, session, expected_version, answers, workflow, messages)
        return confirm_field(db, session, action["field_id"], expected_version, messages)
    else:
        field = _field(session, action["field_id"])
        _check_choice(field, text, action["slot_key"], answers.get(field["id"]))
        from app.services.essay_responses import response_issue, followup
        issue = response_issue(field, action['slot_key'], text)
        if issue or re.search("모르겠|미확인|기억.*안|정확하지", text):
            messages.append({"role": "bot", "text": followup(issue) + "\n" + action['question']})
            return _commit(db, session, expected_version, answers, workflow, messages)
        _set_fact(answers, field, action["slot_key"], text, {"kind": "conversation"})
    _invalidate(workflow)
    following = _action(session, scholarship, answers, workflow, profile)
    messages.append({"role": "bot", "text": next_question(session, following, messages)})
    workflow['last_reply_action'] = action_key(following)
    return _commit(db, session, expected_version, answers, workflow, messages, profile=profile)


def confirm_field(db, session, field_id, expected_version, messages=None):
    scholarship = _active(db, session)
    field = _field(session, field_id)
    answers, workflow = normalize_answers(session), deepcopy(session.workflow or {})
    item = answers.get(field_id, {})
    facts = item.get("facts", {})
    if any(not facts.get(r["key"], {}).get("text") for r in requirements(field, item)):
        raise HTTPException(409, "이 문항의 필요한 정보를 먼저 모아야 합니다.")
    for slot in requirements(field, item):
        _check_choice(field, facts[slot["key"]]["text"], slot["key"], item)
    if duplicate_slot(field, facts):
        raise HTTPException(409, "원문이 요구하는 서로 다른 항목을 확인하세요. 중복된 내용을 수정해야 합니다.")
    for fact in facts.values():
        fact["confirmed"] = True
    item["status"] = "confirmed"
    _invalidate(workflow)
    following = _action(session, scholarship, answers, workflow)
    messages = messages or list(session.messages)
    messages.append({"role": "bot", "text": following["question"] if following else "필요한 정보를 확인했습니다. 문항별 초안을 생성할 수 있습니다."})
    return _commit(db, session, expected_version, answers, workflow, messages)


def edit_fact(db, session, field_id, slot_key, text, expected_version):
    _active(db, session)
    field = _field(session, field_id)
    answers = normalize_answers(session)
    if slot_key not in [r["key"] for r in requirements(field, answers.get(field_id))] and not (field["field_type"] == "essay" and re.fullmatch(r"extra_\d+", slot_key)):
        raise HTTPException(422, "문항의 수집 항목을 선택하세요.")
    text = _clean(text)
    if not text or re.search("모르겠|미확인|기억.*안|정확하지", text):
        raise HTTPException(422, "확인할 수 있는 내용을 입력하세요.")
    from app.services.essay_responses import response_issue, followup
    issue = response_issue(field, slot_key, text)
    if issue:
        raise HTTPException(422, followup(issue))
    _check_choice(field, text, slot_key, answers.get(field_id))
    answers, workflow = normalize_answers(session), deepcopy(session.workflow or {})
    _set_fact(answers, field, slot_key, text, {"kind": "conversation_revision"})
    _invalidate(workflow)
    return _commit(db, session, expected_version, answers, workflow, [*session.messages, {"role": "bot", "text": f"'{field['label']}' 정보를 수정했습니다. 다시 확인해 주세요."}])


def _check_history(db, session, answers, workflow=None):
    active_ids = {f["id"] for f in active_fields(session.fields, workflow if workflow is not None else session.workflow or {})}
    restricted = {f["id"] for f in session.fields if f.get("history_reuse_policy")}
    for identity, item in answers.items():
        if identity not in active_ids:
            continue
        for fact in item.get("facts", {}).values():
            source = fact.get("source", {})
            if source.get("kind") != "history":
                continue
            if identity in restricted:
                raise HTTPException(409, "원문이 이전 신청 내용의 그대로 붙여넣기를 금지합니다. 이번 신청의 내용을 다시 확인하세요.")
            row = db.execute(select(ApplicationDocument, UserApplication).join(UserApplication).where(
                ApplicationDocument.id == source["document_id"], UserApplication.user_id == session.user_id, UserApplication.is_reusable.is_(True))).first()
            if row is None or fact["text"] not in row[0].content_text:
                raise HTTPException(409, "참조 이력이 변경되거나 재사용이 해제되었습니다. 해당 정보를 다시 확인하세요.")


def generate(db, session, expected_version):
    scholarship = _active(db, session)
    answers, workflow = normalize_answers(session), deepcopy(session.workflow or {})
    if _action(session, scholarship, answers, workflow):
        raise HTTPException(409, "자격과 문항별 필요한 정보를 먼저 확인하세요.")
    _check_history(db, session, answers)
    drafts = [compose(field, answers[field["id"]], scholarship) for field in active_fields(session.fields, workflow) if not is_manual(field)]
    if not drafts:
        raise HTTPException(422, "대화로 작성할 문항이 없습니다. 원본에서 직접 작성할 항목을 확인하세요.")
    if any(is_manual(f) for f in active_fields(session.fields, workflow)):
        drafts[0]["validation"]["warnings"].append("manual_original_items_required")
    workflow.update(drafts=drafts)
    workflow.pop("reviewed_digest", None)
    workflow.pop("saved_digest", None)
    text = "문항별 초안을 구성했습니다. 분량과 내용을 확인하고 필요하면 수정해 주세요."
    return _commit(db, session, expected_version, answers, workflow, [*session.messages, {"role": "bot", "text": text}])


def edit_draft(db, session, field_id, text, expected_version):
    _active(db, session)
    field = _field(session, field_id)
    if field.get("choice_spec") or field.get("input_spec"):
        raise HTTPException(422, "세부 항목의 내용은 수집한 정보에서 수정한 뒤 초안을 다시 구성하세요.")
    workflow = deepcopy(session.workflow or {})
    draft = next((d for d in workflow.get("drafts", []) if d["field_id"] == field_id), None)
    if draft is None:
        raise HTTPException(409, "먼저 초안을 생성하세요.")
    text = _clean(text)
    draft.update(text=text, validation=validate_length(field, text), sources=[{"kind": "user_revision"}], reviewed=False, method="user_revision")
    for key in ("reviewed_digest", "saved_digest", "manual_checks_confirmed"):
        workflow.pop(key, None)
    return _commit(db, session, expected_version, normalize_answers(session), workflow, list(session.messages))


def rewrite_draft(db, session, field_id, instruction, expected_version, messages):
    scholarship = _active(db, session)
    field = _field(session, field_id)
    if field['field_type'] != 'essay' or field.get('input_spec') or field.get('choice_spec'):
        raise HTTPException(422, '문체 수정은 서술형 문항에서 사용할 수 있습니다.')
    answers, workflow = normalize_answers(session), deepcopy(session.workflow or {})
    draft = next((d for d in workflow.get('drafts', []) if d['field_id'] == field_id), None)
    if draft is None or answers.get(field_id, {}).get('status') != 'confirmed':
        raise HTTPException(409, '사실 확인과 초안 생성이 먼저 필요합니다.')
    from app.services.essay_generation import generate as generate_prose
    generated = generate_prose(field, answers[field_id]['facts'], scholarship, draft['text'], instruction)
    if generated is None:
        messages.append({'role': 'bot', 'text': '확인한 사실을 유지하는 수정안을 만들지 못해 기존 초안을 보존했습니다. 직접 수정하거나 요청을 바꿔 주세요.'})
        workflow['last_reply_action'] = action_key(_action(session, scholarship, answers, workflow))
        return _commit(db, session, expected_version, answers, workflow, messages)
    draft.update(**generated, validation=validate_length(field, generated['text']), reviewed=False)
    for key in ('reviewed_digest', 'saved_digest', 'manual_checks_confirmed'):
        workflow.pop(key, None)
    messages.append({'role': 'bot', 'text': '확인한 사실을 바탕으로 초안의 문장을 수정했습니다. 내용과 분량을 다시 검토해 주세요.'})
    workflow['last_reply_action'] = action_key(_action(session, scholarship, answers, workflow))
    return _commit(db, session, expected_version, answers, workflow, messages)


def review_draft(db, session, expected_version, manual_checks_confirmed):
    scholarship = _active(db, session)
    workflow = deepcopy(session.workflow or {})
    answers = normalize_answers(session)
    if _action(session, scholarship, answers, workflow) or not workflow.get("drafts"):
        raise HTTPException(409, "필요한 정보 확인과 초안 생성이 먼저 필요합니다.")
    _check_history(db, session, answers)
    groups = validate_groups(workflow.get("form_groups", []), session.fields, workflow["drafts"], workflow)
    checks = _draft_checks(session, workflow["drafts"])
    if any(d["errors"] for d in checks) or any(g["errors"] for g in groups):
        raise HTTPException(409, "분량 초과·부족 또는 미완성 항목을 수정하세요.")
    manual = any(is_manual(f) for f in active_fields(session.fields, workflow))
    if (manual or any(d["warnings"] for d in checks) or any(g["warnings"] for g in groups)) and not manual_checks_confirmed:
        raise HTTPException(409, "원본 양식의 전체 분량·서식과 직접 준비할 사진·첨부·동의·서명 항목을 확인해야 합니다.")
    workflow.update(reviewed_digest=draft_digest(workflow["drafts"]), manual_checks_confirmed=manual_checks_confirmed)
    for draft in workflow["drafts"]:
        draft["reviewed"] = True
    return _commit(db, session, expected_version, answers, workflow, [*session.messages, {"role": "bot", "text": "초안 검토를 완료했습니다. 저장하거나 문항별 작성 자료를 내려받을 수 있습니다."}])


def _draft_checks(session, drafts):
    fields = {f["id"]: f for f in session.fields}
    checks = []
    answers = normalize_answers(session)
    for draft in drafts:
        field = fields[draft["field_id"]]
        if draft.get("skipped") and field.get("required") is False:
            continue
        check = validate_length(field, draft["text"])
        if field.get("choice_spec") or field.get("input_spec"):
            from app.services.form_choice_values import render_choice
            from app.services.form_profile_inputs import render_input
            try:
                render = render_input if field.get("input_spec") else render_choice
                if draft["text"] != render(field, answers.get(field["id"], {})):
                    check["errors"].append("choice_values_do_not_match_confirmed_facts")
            except ValueError:
                check["errors"].append("choice_values_do_not_match_confirmed_facts")
        checks.append(check)
    return checks


def checked_drafts(db, session):
    scholarship = _active(db, session)
    answers, workflow = normalize_answers(session), session.workflow or {}
    drafts = workflow.get("drafts", [])
    if _action(session, scholarship, answers, workflow) or not drafts or workflow.get("reviewed_digest") != draft_digest(drafts):
        raise HTTPException(409, "현재 초안의 검토가 완료되지 않았습니다.")
    _check_history(db, session, answers)
    checks = _draft_checks(session, drafts)
    groups = validate_groups(workflow.get("form_groups", []), session.fields, drafts, workflow)
    if any(d["errors"] for d in checks) or any(g["errors"] for g in groups):
        raise HTTPException(409, "초안 분량과 필수 내용을 확인하세요.")
    if (any(is_manual(f) for f in active_fields(session.fields, workflow)) or any(d["warnings"] for d in checks) or
            any(g["warnings"] for g in groups)) and not workflow.get("manual_checks_confirmed"):
        raise HTTPException(409, "원본에서 직접 처리할 항목을 확인하세요.")
    return scholarship, drafts


def save(db, session, expected_version, allow_reuse):
    scholarship, drafts = checked_drafts(db, session)
    workflow = deepcopy(session.workflow or {})
    digest = draft_digest(drafts)
    application = db.get(UserApplication, session.application_id) if session.application_id else None
    if application and application.user_id != session.user_id:
        raise HTTPException(404, "저장 이력을 찾을 수 없습니다.")
    if application and workflow.get("saved_digest") == digest and application.is_reusable == allow_reuse:
        return view(db, session)
    if application is None:
        application = UserApplication(user_id=session.user_id, scholarship_id=scholarship.id, scholarship_name=scholarship.title,
                                      organization=scholarship.organization, source=ApplicationSource.GENERATED, result=ApplicationResult.DRAFT)
        db.add(application)
    application.is_reusable = allow_reuse
    application.tags = [f"writing-session:{session.id}", f"form-version:{session.form_fingerprint}"]
    application.documents.clear()
    for draft in drafts:
        application.documents.append(ApplicationDocument(doc_type=DocType.OTHER, prompt_question=draft["label"], content_text=draft["text"], char_count=len(draft["text"])))
    db.flush()
    workflow["saved_digest"] = digest
    return _commit(db, session, expected_version, normalize_answers(session), workflow,
                   [*session.messages, {"role": "bot", "text": "검토한 초안을 내 신청서 이력에 저장했습니다."}], application_id=application.id)
