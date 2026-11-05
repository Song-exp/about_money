"""정보 대화에서 추천과 작성 페이지 선택만 담당한다."""
import logging
import re
import time
from fastapi import HTTPException
from sqlalchemy import select
from app.models.scholarship import Scholarship, Category
from app.services import chat
from app.services.information_responses import generate_reply
from app.services.conversation_intent import interpret_turn, apply_request, effective_profile, FIELDS
from app.services.information_tools import execute
from app.services import information_store
from app.services.consultation_scope import scope_clause, in_scope, policy_period
from app.services.matching import match_scholarships, evaluate_eligibility, matches_benefit_type
from app.services.scholarship_readiness import readiness

get_or_create_session = chat.get_or_create_session
logger = logging.getLogger(__name__)

def candidates(db, sess):
    profile = effective_profile(sess)
    results = match_scholarships(db, profile, limit=sess.search_limit,
        benefit_type=sess.benefit_type if sess.consultation == 'scholarship' else None,
        query=sess.search_query, consultation=sess.consultation)
    sess.candidates = [r.scholarship.id for r in results]
    found = []
    for i, r in enumerate(results):
        found.append(candidate_detail(db.get(Scholarship, r.scholarship.id), sess, i + 1, r.reasons))
    sess.last_search_candidates = found
    return found


def candidate_detail(row, sess, index, reasons=None, include_body=False):
        eligibility = evaluate_eligibility(row, sess.profile)
        checks = eligibility["checks_needed"]
        if sess.search_mode == "browse":
            checks = ["전체 공고 둘러보기 · 개인 자격 조건은 별도 확인", *checks]
        elif sess.ignored_profile_fields:
            checks = [f"검색에서 제외한 조건: {' · '.join(FIELDS[key] for key in sorted(sess.ignored_profile_fields))} · 실제 신청 자격은 별도 확인", *checks]
        result = {"index": index, "id": row.id, "title": row.title,
             "deadline": row.deadline_at.date().isoformat() if row.deadline_at else None,
             "source_url": row.source_url, "benefit": row.benefit,
             "eligibility": {key: value for key, value in (row.eligibility or {}).items()
                             if key in ('income_bracket', 'gpa_min', 'region', 'grade_level', 'major', 'target_desc', 'scope')},
             "reasons": reasons if reasons is not None else eligibility['reasons'],
             "eligibility_status": eligibility["status"],
             "checks_needed": checks, "readiness": row.readiness}
        result['consultation'] = sess.consultation
        result['source_requirements'] = ((row.eligibility or {}).get('policy_validation' if sess.consultation == 'youth_policy' else 'document_validation') or {}).get('requirements', [])
        if sess.consultation == 'youth_policy':
            result['application_period'] = policy_period(row)
            result['deadline'] = (result['application_period']['end_at'] or '')[:10] or None
            result['readiness'] = {**result['readiness'], 'writing_ready': False}
        if include_body:
            result.update(organization=row.organization, required_documents=row.required_documents,
                          body_excerpt=(row.body_text or '')[:12000])
        return result




def _empty_message(db, sess):
    rows = db.scalars(select(Scholarship).where(scope_clause(sess.consultation))).all()
    available = [s for s in rows if readiness(s)["recommendation_ready"]]
    filtered = [s for s in available if not sess.benefit_type or matches_benefit_type(s, sess.benefit_type)]
    label = {"living": "생활비성 ", "tuition": "등록금 "}.get(sess.benefit_type, "")
    if sess.consultation == 'youth_policy':
        return ('현재 이 상담에서 안내할 수 있는 청년정책이 없습니다. 지역 조건을 바꾸거나 전체 정책을 둘러볼 수 있어요.'
                if available else '지금 안내할 수 있는 청년정책 자료가 없어요. 자료가 갱신되면 다시 확인해 주세요.')
    if not rows:
        return "아직 등록된 장학금 공고가 없어요. 새 공고가 등록되면 안내해 드릴게요."
    if not available:
        return "지금 안내할 수 있는 모집 중 장학금이 없어요. 조건을 더 입력하지 않으셔도 돼요."
    if not filtered:
        return f"현재 검색 가능한 장학금은 {len(available)}건이지만, 지급 설명이나 제목에서 {label.strip()} 지원이 확인된 모집 공고는 없습니다. 지급 유형이 미확인인 공고를 임의로 포함하지 않았어요. 다른 유형도 보려면 ‘모든 유형의 장학금 보여줘’라고 말씀해 주세요."
    conditions = []
    applied_profile = effective_profile(sess)
    for key, name in (("income_bracket", "소득분위"), ("gpa", "학점"), ("grade_level", "학년"), ("region", "거주 지역"), ("major", "전공")):
        value = getattr(applied_profile, key)
        if value is not None:
            conditions.append(f"{name} {value}")
    applied = f"적용한 조건: {' · '.join(conditions)}.\n" if conditions else ""
    return (applied + f"현재 검색 가능한 {label}장학금 {len(filtered)}건 중 입력한 조건과 일치하는 공고는 없습니다. "
            "추가 정보를 반복 입력하실 필요는 없어요. ‘전체 모집 공고 10개 보여줘’라고 하시면 개인 자격을 적용하지 않은 목록을 확인할 수 있습니다. 목록은 이 서비스에 등록된 공고 기준입니다.")


def validate_selection(db, session_id, user_id, scholarship_id):
    with chat._SESSIONS_LOCK:
        sess = chat._SESSIONS.get(session_id)
        if not sess or time.monotonic() - sess.last_seen > chat.SESSION_IDLE_SEC or (sess.user_id and sess.user_id != user_id):
            raise HTTPException(409, "추천 대화가 만료되었거나 접근할 수 없습니다. 다시 검색하세요.")
        if scholarship_id not in sess.candidates:
            raise HTTPException(409, "현재 추천 목록에서 장학금을 선택하세요.")
        if user_id and sess.user_id is None:
            sess.user_id = user_id
    scholarship = db.get(Scholarship, scholarship_id)
    if sess.consultation != 'scholarship' or (scholarship and not in_scope(scholarship, 'scholarship')):
        raise HTTPException(409, '청년정책은 정책 상담에서 신청 조건과 원문을 확인해 주세요. 장학금 작성 대상으로 사용할 수 없습니다.')
    if not scholarship or not readiness(scholarship)["recommendation_ready"] or evaluate_eligibility(scholarship, sess.profile)["status"] == "not_eligible":
        raise HTTPException(409, "공고 또는 조건이 바뀌었습니다. 다시 검색하세요.")
    return sess, scholarship


def handle_message(db, session_id, user_id, text, consultation='scholarship'):
    information_store.restore(db, session_id, user_id, consultation)
    sess = get_or_create_session(session_id, user_id)
    if session_id and sess.consultation != consultation:
        raise HTTPException(409, '다른 종류의 상담 대화입니다. 현재 상담에서 새 대화를 시작하세요.')
    sess.consultation = consultation
    with sess.turn_lock:
        return _handle_message(db, sess, user_id, text)


def _handle_message(db, sess, user_id, text):
    if sess.consultation == 'youth_policy':
        return _handle_policy_message(db, sess, text)
    chat._load_profile_from_account(db, sess)
    text = (text or "").strip()
    selection = None
    number = re.fullmatch(r"\s*(\d+)\s*(?:번)?\s*", text)
    if sess.consultation == 'scholarship' and number and 0 < int(number[1]) <= len(sess.candidates):
        scholarship_id = sess.candidates[int(number[1]) - 1]
        validate_selection(db, sess.session_id, user_id, scholarship_id)
        selection = {"scholarship_id": scholarship_id}
        found = [candidate_detail(db.get(Scholarship, scholarship_id), sess, int(number[1]))]
        sess.last_tool_result = {'tool': 'select', 'returned_ids': [scholarship_id], 'executed': True}
    else:
        request = interpret_turn(sess, text)
        changed = apply_request(sess, request)
        if changed:
            chat._save_profile_to_account(db, sess)
        found, selection = execute(db, sess, request)
    sess.state = chat.ChatState.SELECT if found else chat.ChatState.COLLECT
    message = ("선택한 장학금의 신청서 작성 페이지로 이동합니다." if selection else
               "공고를 바탕으로 찾은 후보입니다. 추가 확인이 필요한 자격도 살펴보세요. 신청하고 싶은 장학금 카드를 선택해 주세요." if found else
               _empty_message(db, sess))
    if found and not selection:
        label = {"living": "생활비성 ", "tuition": "등록금 "}.get(sess.benefit_type, "")
        if sess.search_mode == "browse":
            message = f"개인 조건을 적용하지 않고, 모집 중인 {label}장학금 {len(found)}건을 가져왔어요. 각 공고의 지역·추가 자격을 확인해 주세요."
        else:
            message = f"입력 조건으로 찾은 {label}장학금 {len(found)}건입니다. 추가 확인이 필요한 자격을 살펴보고 신청할 카드를 선택해 주세요."
        if len(found) < sess.search_limit:
            message += f" 요청 범위에서 안내 가능한 공고는 {len(found)}건이라 {sess.search_limit}건을 채우기 위해 다른 공고를 넣지 않았어요."
        if sess.consultation == 'youth_policy':
            message = f'청년정책 자료 {len(found)}건입니다. 지원 대상·신청 방법을 확인해 보세요. 현재 접수 여부와 기간은 각 원문에서 확인해야 합니다.'
    # Model/API failure only: retain a grounded fallback with actual DB values.
    if found and re.search(r"마감|금액|얼마|비교|지원금|혜택|자격|대상", text) and not selection:
        lines = []
        for candidate in found:
            benefit = candidate["benefit"]
            amount = benefit.get("amount_desc") or (f"{benefit['amount_krw']:,}원" if isinstance(benefit.get("amount_krw"), (int, float)) else "지급액 미확인")
            reasons = " · ".join(candidate["reasons"] + candidate["checks_needed"]) or "추가 자격 확인 필요"
            lines.append(f"{candidate['title']} · 신청 마감 {candidate['deadline'] or '미확인'} · {amount}\n{reasons}\n공고 원문: {candidate['source_url']}")
        message = "\n\n".join(lines) + ('\n정책 상세와 공식 신청 방법을 확인해 주세요.' if sess.consultation == 'youth_policy' else "\n신청하고 싶은 장학금 카드를 선택해 주세요.")
    if found and sess.consultation == 'youth_policy' and sess.last_tool_result.get('tool') in ('detail', 'compare'):
        message = '\n\n'.join(f"{item['title']}\n{item.get('body_excerpt') or '세부 신청 방법은 원문에서 확인해 주세요.'}\n"
                              f"{item['application_period']['label']}\n정책 원문: {item['source_url']}" for item in found)
    labels = {"income_bracket": "소득분위", "gpa": "학점", "grade_level": "학년", "region": "거주 지역", "major": "전공"}
    missing = [key for key in labels if getattr(sess.profile, key) is None and key not in sess.ignored_profile_fields]
    ask = missing[0] if missing else None
    if ask and found and sess.search_mode == "personal" and not selection and sess.consultation == 'scholarship':
        message += f"\n{labels[ask]}을 알려주시면 후보를 더 좁힐 수 있어요."
    answer_source, fallback_reason = "system", None
    from app.services.information_responses import evidence_context
    context = evidence_context(db, sess, found)
    empty_catalog = context['catalog']['collected'] and not context['catalog']['searchable']
    if not selection and not empty_catalog:
        message, answer_source, fallback_reason = generate_reply(db, sess, text, found, message)
    if sess.consultation == 'scholarship':
        if empty_catalog:
            message = "지금 안내할 수 있는 모집 중 장학금이 없어요. 새 공고가 확인되면 안내해 드릴게요."
            answer_source, fallback_reason = 'system', None
    sess.history.extend([("user", text), ("bot", message)])
    sess.history = sess.history[-40:]
    reply = chat.BotReply(session_id=sess.session_id, state=sess.state.value, message=message,
                          candidates=found, profile=sess.profile.model_dump(),
                          answer_source=answer_source, fallback_reason=fallback_reason,
                          search={'consultation': sess.consultation, 'mode': sess.search_mode, 'benefit_type': sess.benefit_type if sess.consultation == 'scholarship' else None, 'limit': sess.search_limit,
                                  'ignored_fields': sorted(sess.ignored_profile_fields), 'filters': sess.search_profile_filters,
                                  'query': sess.search_query, 'effective_profile': effective_profile(sess).model_dump()},
                          tool_result=sess.last_tool_result, citations=sess.last_answer_citations if not selection else [])
    reply.selection = selection
    sess.turns.extend([{'role': 'user', 'text': text}, {'role': 'bot', 'text': message,
        'candidates': found, 'answer_source': answer_source, 'fallback_reason': fallback_reason,
        'search': reply.search, 'citations': reply.citations}])
    sess.turns = sess.turns[-40:]
    information_store.save(db, sess)
    return reply


def _handle_policy_message(db, sess, text):
    from app.services.policy_intake import generate, period_status
    chat._load_profile_from_account(db, sess)
    questions, rows, source, reason, total = generate(db, sess, text)
    found = [candidate_detail(row, sess, i + 1, reasons=['관련 정책 자료 · 자격 확인 필요'], include_body=True)
             for i, row in enumerate(rows)]
    for candidate in found:
        # Scholarship-style GPA/income matching never decides policy eligibility.
        candidate['eligibility_status'] = 'unknown'
        candidate['checks_needed'] = ['공고별 자격과 신청 기간 확인 필요']
    current = [r for r in rows if period_status(r) == 'current']
    intro = ('최근 확인한 신청 기간 기준으로 접수 기간에 해당하는 자료를 포함했습니다. 개인 자격은 추가 확인이 필요합니다.'
             if current else '현재 접수 중임을 확인한 공고는 없습니다. 아래는 정책 자료의 자격 조건을 확인하는 사전 상담입니다.')
    lines = [intro]
    by_id = {r.id: r for r in rows}
    if questions:
        lines.append('공고의 지원 조건을 바탕으로 먼저 여쭤볼게요. 해당되는 내용만 답해 주세요.')
        lines.extend(f'{i}. {q.text}\n관련 정책: {by_id[q.policy_id].title}' for i, q in enumerate(questions, 1))
    elif rows:
        if source != 'llm':
            lines.append('질문 생성 모델을 사용할 수 없어 공고 기반 문진을 진행하지 못했습니다. 관심 정책을 알려주시면 자료를 확인할 수 있습니다.')
        else:
            lines.append('아래 관련 정책 자료에서 지원 대상과 신청 방법을 확인해 주세요.')
        if text:
            lines.extend(f"{r.title}\n{(r.body_text or '상세 조건은 원문 확인 필요')[:6500]}\n원문: {r.source_url}" for r in rows[:5])
    else:
        collected = db.scalars(select(Scholarship).where(scope_clause('youth_policy'))).all()
        lines.append('관련 후보를 선정하지 못했습니다. 관심 분야나 정책 이름을 알려주세요.' if total else
                     '지금 안내할 수 있는 청년정책 자료가 없어요. 자료가 확인되면 다시 안내해 드릴게요.' if collected else
                     '질문에 답할 수 있는 정책 자료가 없어요. 자료가 갱신되면 다시 확인해 주세요.')
    message = '\n\n'.join(lines)
    sess.candidates = [r.id for r in rows]
    sess.last_search_candidates = found
    sess.last_answer_citations = sess.candidates[:]
    sess.last_tool_result = {'tool': 'policy_intake', 'returned_ids': sess.candidates, 'executed': True,
                            'catalog_count': total, 'sample_limit': 32,
                            'questions': [q.model_dump() for q in questions]}
    sess.state = chat.ChatState.SELECT if found else chat.ChatState.COLLECT
    if text:
        sess.history.append(('user', text))
        sess.turns.append({'role': 'user', 'text': text})
    sess.history.append(('bot', message))
    search = {'consultation': 'youth_policy', 'mode': 'policy_intake', 'limit': 32,
              'benefit_type': None, 'ignored_fields': [], 'effective_profile': {}, 'query': ''}
    sess.turns.append({'role': 'bot', 'text': message, 'candidates': found, 'answer_source': source,
                       'fallback_reason': reason, 'citations': sess.candidates[:], 'search': search})
    sess.history, sess.turns = sess.history[-40:], sess.turns[-40:]
    information_store.save(db, sess)
    return chat.BotReply(session_id=sess.session_id, state=sess.state.value, message=message,
                         candidates=found, profile=sess.profile.model_dump(), answer_source=source,
                         fallback_reason=reason, search=search, citations=sess.candidates[:],
                         tool_result=sess.last_tool_result)


def note_upload(db, session_id, user_id, filename, text_len):
    information_store.restore(db, session_id, user_id)
    sess = get_or_create_session(session_id, user_id)
    chat._load_profile_from_account(db, sess)
    found = candidates(db, sess)
    reply = chat.BotReply(session_id=sess.session_id, state="select" if found else "collect",
                         message=f"'{filename}'의 본문 {text_len:,}자를 이력에 보관했습니다. 장학금을 선택한 뒤 별도 작성 대화에서 활용할 수 있습니다.",
                         candidates=found, profile=sess.profile.model_dump())
    user_text = f'신청서 첨부 · {filename}'
    sess.history.extend([('user', user_text), ('bot', reply.message)])
    sess.history = sess.history[-40:]
    sess.turns.extend([{'role': 'user', 'text': user_text}, {'role': 'bot', 'text': reply.message, 'candidates': found}])
    sess.turns = sess.turns[-40:]
    information_store.save(db, sess)
    return reply
