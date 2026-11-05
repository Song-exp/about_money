"""Preserve official API responses and stable, complete policy text without OCR."""
import hashlib
import json
import re
import math
from pathlib import Path

CODEBOOK = Path('data/youth_policy_codes.json')
CODE_FIELDS = ('mrgSttsCd', 'earnCndSeCd', 'jobCd', 'schoolCd', 'plcyMajorCd', 'sbizCd')
CODE_PREFIXES = dict(mrgSttsCd='0055', earnCndSeCd='0043', jobCd='0013', schoolCd='0049', plcyMajorCd='0011', sbizCd='0014')
VOLATILE = {'inqCnt', 'frstRegDt', 'lastMdfcnDt'}
LABELS = dict(plcyNm='정책명', plcyExplnCn='설명', plcySprtCn='지원내용',
    addAplyQlfcCndCn='신청자격', ptcpPrpTrgtCn='참여제한대상', plcyAplyMthdCn='신청방법',
    srngMthdCn='심사방법', sbmsnDcmntCn='제출서류', etcMttrCn='기타사항',
    sprtTrgtMinAge='지원대상최소연령', sprtTrgtMaxAge='지원대상최대연령', sprtTrgtAgeLmtYn='연령제한여부',
    mrgSttsCd='혼인상태코드', earnCndSeCd='소득조건코드', earnMinAmt='소득최소금액',
    earnMaxAmt='소득최대금액', earnEtcCn='소득기타조건', jobCd='취업상태코드', schoolCd='학력코드',
    plcyMajorCd='전공코드', sbizCd='특화요건코드', aplyYmd='신청기간', aplyPrdSeCd='신청기간구분코드',
    bizPrdBgngYmd='사업시작일', bizPrdEndYmd='사업종료일', bizPrdEtcCn='사업기간기타내용',
    aplyUrlAddr='신청URL', refUrlAddr1='참고URL1', refUrlAddr2='참고URL2', zipCd='지역코드',
    sprtSclCnt='지원규모', sprtSclLmtYn='지원규모제한여부', sprtArvlSeqYn='선착순여부')


def parse_codebook(content):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(content, 'lxml')
    codes, conflicts = {}, set()
    for tag in soup.select('input[value], option[value]'):
        value = tag.get('value', '')
        if not re.fullmatch(r'\d{7}', value):
            continue
        label = soup.find('label', attrs={'for': tag.get('id')}) if tag.get('id') else None
        if label is None:
            label = tag.find_parent('label')
        if label is None and tag.name == 'option':
            label = tag
        text = ' '.join(label.get_text(' ', strip=True).split()) if label else ''
        if not text or len(text) > 80:
            continue
        if value in codes and codes[value] != text:
            conflicts.add(value)
        codes[value] = text
    return {k: v for k, v in codes.items() if k not in conflicts}


def parse_display_rules(content):
    text = content.decode('utf-8')
    rules = {}
    income = re.search(r"if \(plcyList\.earnCndSeCd === '0043001'\)[\s\S]{0,700}?earnEtcCn[\s\S]{0,100}?\n\s*}", text)
    if income and "earnCn = '무관'" in income[0] and "'0043002'" in income[0] and '만원 이상' in income[0] and '만원 이하' in income[0]:
        rules['income'] = {'version': 'official-income-display-v1', 'evidence': income[0]}
    age = re.search(r"if \(plcyList\.sprtTrgtAgeLmtYn === 'Y'\)[\s\S]{0,200}?}\s*else\s*{[\s\S]{0,300}?\n\s*}", text)
    if age and "trgtAge = '제한없음'" in age[0] and 'sprtTrgtMinAge' in age[0] and 'sprtTrgtMaxAge' in age[0]:
        rules['age'] = {'version': 'official-age-display-v1', 'evidence': age[0]}
    return rules


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def valid_range(low, high, *, integer=False):
    try:
        a, b = float(low), float(high)
        return math.isfinite(a) and math.isfinite(b) and 0 <= a <= b and (not integer or a.is_integer() and b.is_integer() and b > 0)
    except (TypeError, ValueError):
        return False


def text_units(normalized):
    units = [{'unit_id': f'api:field:{k}', 'text': f'[{LABELS.get(k, k)}] {v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}'}
             for k, v in sorted(normalized['api_fields'].items()) if v is not None and str(v).strip()]
    units += [{'unit_id': f'api:decoded:{field}:{index}',
               'text': f'[{LABELS.get(field, field)} 코드명] {x["code"]}: {x["label"]}'}
              for field, values in normalized['decoded_conditions'].items() for index, x in enumerate(values)]
    return units


def api_documents(item, url):
    book = json.loads(CODEBOOK.read_text(encoding='utf-8')) if CODEBOOK.is_file() else {}
    codes = book.get('codes', {})
    rules = book.get('rules', {})
    semantic = {k: v for k, v in item.items() if k not in VOLATILE}
    decoded, unresolved = {}, []
    for field in CODE_FIELDS:
        values = [v.strip() for v in str(item.get(field) or '').split(',') if v.strip()]
        decoded[field] = []
        for value in values:
            if not value.startswith(CODE_PREFIXES[field]):
                unresolved.append({'field': field, 'code': value, 'reason': 'wrong_code_group'})
                continue
            if field == 'earnCndSeCd' and rules.get('income'):
                label = ('소득 무관' if value == '0043001' else
                         f"연소득 {item.get('earnMinAmt')}만원 이상 ~ {item.get('earnMaxAmt')}만원 이하" if value == '0043002' and valid_range(item.get('earnMinAmt'), item.get('earnMaxAmt')) else
                         f"기타 소득조건: {item['earnEtcCn']}" if value == '0043003' and str(item.get('earnEtcCn') or '').strip() else None)
                if label:
                    decoded[field].append({'code': value, 'label': label})
                else:
                    unresolved.append({'field': field, 'code': value})
            elif value in codes:
                decoded[field].append({'code': value, 'label': codes[value]})
            else:
                unresolved.append({'field': field, 'code': value})
    flag = str(item.get('sprtTrgtAgeLmtYn') or '').strip()
    if flag:
        if rules.get('age') and (flag == 'Y' or flag == 'N' and valid_range(item.get('sprtTrgtMinAge'), item.get('sprtTrgtMaxAge'), integer=True)):
            decoded['sprtTrgtAgeLmtYn'] = [{'code': flag, 'label': '연령 제한없음' if flag == 'Y' else
                f"만 {item.get('sprtTrgtMinAge')}세 ~ 만 {item.get('sprtTrgtMaxAge')}세"}]
        else:
            unresolved.append({'field': 'sprtTrgtAgeLmtYn', 'code': flag})
    normalized = {'api_fields': semantic, 'decoded_conditions': decoded}
    text = '\n'.join(unit['text'] for unit in text_units(normalized))
    raw, stable = encoded(item), encoded(normalized)
    source = {'semantic_sha256': hashlib.sha256(stable).hexdigest(),
              'response_sha256': hashlib.sha256(raw).hexdigest(), 'unresolved_codes': unresolved,
              'codebook_source': book.get('source_url'), 'codebook_sha256': book.get('sha256'),
              'policy_id': str(item.get('plcyNo') or '')}
    documents = [
        {'kind': 'policy_api_response', 'url': url, 'filename': 'policy-response.json', 'content': raw, 'status': 'parsed'},
        {'kind': 'policy_api_text', 'url': url, 'filename': 'policy-semantic.json', 'content': stable,
         'status': 'parsed', 'extracted_text': text,
         'blocks': [{'text': text, 'start': 0, 'end': len(text), 'location': 'api:policy', 'method': 'api_text'}]}]
    return text, source, documents
