"""Evidence-bound questions for unresolved issuer policy; never an override."""
import hashlib

from app.models.card_collection import CardCollectionSource,CardSourceSnapshot
from app.models.card_document_text import CardDocumentText
from app.services.card_document_crosscheck import TEXT_PARSER
from app.services.card_publication import payload_hash

LABELS={
    'spend.higher_label':('높은 실적 구간 문구','생활서비스 실적 구간과 한도 판단'),
    'outside_scope.cinema.brand':('영화관 안내','현재 매장 규칙 범위 밖의 영화 서비스'),
}


def policy_review_packet(db,version,comparison):
    if version.program_key not in {'hana_day_offline','shinhan_deep_oil_living'}:
        return None
    primary=db.get(CardSourceSnapshot,version.snapshot_id)
    source=db.get(CardCollectionSource,version.source_id)
    secondary=db.get(CardSourceSnapshot,comparison.get('snapshot_id')) if comparison.get('snapshot_id') else None
    derived=db.get(CardDocumentText,comparison.get('text_id')) if comparison.get('text_id') else None
    if derived and (not secondary or derived.snapshot_id!=secondary.id or derived.parser_version!=TEXT_PARSER):
        derived=None
    verified=(comparison['status'] in {'conflict','partial_match'} and derived is not None
        and hashlib.sha256(primary.extracted_text.encode()).hexdigest()==primary.semantic_sha256
        and payload_hash(version.payload)==version.payload_hash)
    fields=[]
    def display(value):
        if type(value) is bool:
            return '할인 제외 목록에 명시' if value else '해당 제외 목록에 미기재(할인 허용으로 확정하지 않음)'
        return str(value)
    for row in comparison.get('conflicts',[]) if verified else []:
        field=row['field']
        label,impact=LABELS.get(field,(field.removeprefix('discount_exclusions.'),'공통 할인 제외 여부'))
        fields.append({**row,'label':label,'impact':impact,'resolution':'unresolved',
            'question':f'{label}: 상품 상세의 {display(row["detail_value"])}과 공식 안내장의 {display(row["leaflet_value"])} 중 적용 기준은 무엇인가요? 적용일·대상 카드·예외 조건과 공식 근거를 확인해 주세요.'})
    name='토스뱅크 하나카드 Day' if version.program_key=='hana_day_offline' else '신한카드 Deep Oil'
    questions=[row['question'] for row in fields]
    if version.program_key=='hana_day_offline':
        questions.extend([
            '아파트관리비·사회보험료 자동납부 혜택과 공통 할인 제외 목록의 적용 관계를 확인해 주세요. 자동납부에 대한 별도 예외인지, 문구 정정이 필요한지 적용 근거를 확인해 주세요.',
            '10% 할인 대상 결제 10,005원의 할인액 1,000.5원은 어떻게 처리하나요? 거래별 버림·반올림·월 합산 등 계산 단위와 규칙을 알려 주세요.',
            '영역별 한도와 8개 영역 통합 한도 중 잔여 한도가 거래 할인액보다 작을 때 잔여 한도만큼 부분 할인되는지, 해당 거래가 제외되는지 확인해 주세요.',
        ])
    else:
        questions.extend([
            '편의점·커피의 현장 결제, 공식 앱 주문, 선불 충전 후 사용, 간편결제별 할인 대상 여부와 카드사 가맹점 인식 기준을 확인해 주세요. 택시의 오프라인 제한을 다른 영역에 동일 적용해도 되는지 구분해 주세요.',
            '5% 할인 대상 결제 10,010원의 할인액 500.5원은 어떻게 처리하나요? 거래별 버림·반올림·월 합산 등 계산 단위와 규칙을 알려 주세요.',
            '가족카드·편의점·커피·택시 합산 잔여 할인 대상 이용금액이 300원일 때 10,000원을 결제하면, 300원에 대한 15원 할인인지 거래 전체 제외인지 다른 처리인지 확인해 주세요. 취소 시 한도 복원도 함께 확인해 주세요.',
        ])
    questions.append('상품 상세와 안내장의 적용 대상·시행일·개정 이력 및 혜택 종료/변경 조건을 확인해 주세요. 공시 게시일이나 심의일만으로 혜택 시행일을 판단할 수 있는지도 구분해 주세요.')
    packet={'status':'unresolved','program_key':version.program_key,'version_id':version.id,
        'payload_hash':version.payload_hash,'comparison_status':comparison['status'],
        'comparison_evidence_verified':verified,
        'primary':{'source_url':source.canonical_url,'snapshot_id':primary.id,'raw_sha256':primary.raw_sha256,
                   'text_sha256':primary.semantic_sha256},
        'supporting_document':{'source_url':comparison.get('source_url'),
            'snapshot_id':secondary.id if secondary else None,'raw_sha256':secondary.raw_sha256 if secondary else None,
            'text_id':derived.id if derived else None,'text_sha256':derived.text_sha256 if derived else None},
        'conflicts':fields,'questions':questions,
        'required_response':'공식 개정 문서 또는 적용 대상·시행일·예외·계산 방식이 명시된 카드사의 확인 근거',
        'effect':'확인 요청서 작성만 수행하며 승인·게시·정책·사용자 정보를 변경하지 않음'}
    lines=[name+' 혜택 조건 확인 요청서',f'프로그램: {version.program_key} / 추출 버전: {version.id}',
        f'조건 지문: {version.payload_hash}',f'상품 상세: {source.canonical_url}',
        f'공식 안내장 경로: {comparison.get("source_url", "확인 필요")}',
        f'상품 원문 지문: {primary.raw_sha256}',
        f'안내장 원문 지문: {secondary.raw_sha256 if secondary else "미확보"}',
        f'교차 검증 상태: {comparison["status"]}',
        '아래 불일치와 미확인 조건에 대해 적용 대상·시행일이 포함된 공식 근거를 요청합니다.',
        *[f'{i}. {q}' for i,q in enumerate(questions,1)],
        '참고: 이 문서는 문의 초안입니다. 문의 발송·카드사 답변 수신·조건 해결·게시 승인을 의미하지 않습니다.']
    packet['inquiry_text']='\n\n'.join(lines)
    return packet
