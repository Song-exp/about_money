"""서술 응답의 명시적인 확인/미확인/추가 요청을 사실과 구분한다."""
import re

ACK = re.compile(r"^(?:네|예|응|넵|확인(?:했습니다|했어요|해요|완료)?|맞(?:습니다|아요)|알겠습니다|알겠어요|좋(?:습니다|아요)|감사합니다|모두해당(?:합니다)?)[,.!。\s]*$")
ABSENCE = {'해당없음','없음','없습니다','해당사항없음','해당사항없습니다'}


def response_issue(field, key, text):
    if field.get('field_type') != 'essay':
        return None
    if ACK.fullmatch(text.strip()):
        return 'acknowledgement_without_fact'
    if re.search(r'모르겠|미확인|기억.*안|정확하지|아직\s*확인하지|확인\s*필요', text):
        return 'unverified_response'
    if re.fullmatch(r'(?:장학금\s*(?:추천|알려)|신청\s*방법\s*알려|(?:질문|문항|예시)\s*(?:설명|알려))\s*(?:줘|주세요|줄래|줄래요|해줘|해주세요)[?!.\s]*', text):
        return 'request_without_fact'
    compact = re.sub(r'\s', '', text).strip('.!。')
    if key.startswith('original_detail_'):
        # 실제 기관명/짧은 빈도도 허용한다. 글자 수를 의미 충족의 판정으로 쓰지 않는다.
        if compact in ABSENCE:
            return None
        if len(compact) < 2 or not re.search(r'[가-힣A-Za-z0-9]', compact):
            return 'response_too_short'
    elif len(text.strip()) < 8:
        return 'response_too_short'
    return None


def followup(issue):
    if issue == 'acknowledgement_without_fact':
        return "확인 응답만으로는 이 항목을 채울 수 없습니다. 질문에 대한 본인의 실제 내용을 알려주세요."
    if issue == 'unverified_response':
        return "아직 확인되지 않은 내용은 초안에 넣지 않겠습니다. 사실을 확인한 뒤 답해 주세요."
    if issue == 'request_without_fact':
        return "이 항목의 본인 정보가 아직 수집되지 않았습니다. 원문 질문에 대한 실제 내용을 알려주세요."
    return "이 항목에 넣을 본인 내용을 조금 더 구체적으로 알려주세요."
