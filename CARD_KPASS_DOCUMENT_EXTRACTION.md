# K-패스 접힌 상세 안내 복원

2026-10-17: [공식 K-패스 페이지](https://www.tossbank.com/card/k-pass)의 원본 HTML 안에 들어 있는 접힌 안내 텍스트를 파싱해 별도 보관하도록 연결했다. 혜택 규칙 추출·승인·지도 적용까지 완료한 단계는 아니다.

## 복원 범위

기존 스냅샷 110의 읽기 가능한 요약 438자는 그대로 보존했다. 같은 HTML의 `__NEXT_DATA__`에서 해당 페이지의 공개 `TCSDisclaimerResource`를 식별해 상품 안내·K-패스 안내·온라인 결제 안내·기타 안내를 복원했다.

최종 파생 텍스트는 **5,462자, 4개 영역·93개 블록·96개 문구 근거**다. 텍스트 조각을 이어 붙일 때 원문에 없는 단어 사이 공백을 넣지 않는다. 각 문구는 JSON Pointer와 파생 텍스트의 문자 범위에 연결한다. 표는 셀 내용을 순서대로 보존하고 각 문구의 원본 셀 경로를 유지한다.

원본 HTML의 지문과 파서 `toss_kpass_hydration_v1`에 연결해 기존 `CardDocumentText` 테이블의 파생 텍스트 3번으로 저장했다. 같은 원문·파서를 재처리하면 중복 생성하지 않는다. 원문이나 기존 요약을 덮어쓰거나 새 수집 시각을 만들어내지 않았다. 실제 원문 관찰 시각은 2026-10-16 20:02 KST이며 이번 작업은 그 보관 파일의 재처리다.

상품안내서·개인회원 약관을 가리키는 참조 2개도 별도로 식별했다. 원문 값 `60303005`, `60303001`은 문서 식별자다. 이를 URL로 추측해 요청하지 않았으며, 두 연결 문서는 `not_collected`로 표시한다. 접힌 안내를 복원했다고 전체 약관 수집 완료로 표시하지 않는다.

## 실패 처리와 파이프라인

새 파서는 정확한 페이지 경로·공개 게시 상태·리소스 식별자·4개 안내 영역을 확인한다. 중복 리소스·중복 블록·텍스트 필드 충돌·누락된 자식·표 너비 불일치·지원하지 않는 이미지/블록은 실패 처리한다. 무시하고 부분 결과를 완료로 표시하지 않는다. 블록 수·깊이·본문 크기도 제한한다.

기존 예약 수집에서 K-패스의 전체 HTTP 200 본문을 수집하면 상세 파생 텍스트도 생성한다. 304 응답으로 새 상세 텍스트나 신선도를 만들지 않는다. 보관 원문은 다음 명령으로 외부 요청 없이 재처리할 수 있다.

```powershell
.venv\Scripts\python.exe -X utf8 -m app.scripts.collect_card_sources --reparse-documents --document-review-output reports/card-documents/checks.json --output reports/card-documents/state.json
```

기존 Day·Deep Oil PDF 복원도 같은 명령에서 처리한다. Day·Deep Oil의 충돌 검증 결과와 승인 차단은 유지한다.

## 검토 API와 결과 파일

관리자 인증이 필요한 `GET /admin/map/card-sources/7/document`에서 최신 관찰 스냅샷에 연결된 요약·상세 텍스트·지문·관찰 시각·출처 상태를 조회한다. 지문이 다르거나 재처리가 필요하면 거부한다. 현재 이 API는 K-패스 상세 안내 검토만 지원한다. 오래된 보관 원문을 현재 유효한 혜택으로 보증하는 API가 아니다.

실제 복원 결과:

- [상세 안내 텍스트](reports/kpass-document-20261017/kpass-disclaimer.txt)
- [문구별 원문 위치와 미수집 참조](reports/kpass-document-20261017/evidence.json)

`benefit_rules_not_extracted`, `not_publicly_approved`, `linked_documents_not_collected`를 명시한다. 추가 카드·혜택·보유 카드 연결이나 실제 결제는 생성하지 않았다.

## 검증

관련 테스트 82개, 전체 테스트 **1,639개** 통과. 실패·건너뜀 없음. 실제 로컬 API에서 파생 텍스트를 읽고 아카이브 파일의 SHA-256, 재파싱 결과, 96개 원문 위치와 일치하는지 확인했다. 로컬 백엔드와 예약 수집도 새 코드로 실행했다. 프런트엔드는 변경하지 않아 빌드·브라우저 검사를 반복하지 않았다. 운영 배포는 하지 않았다.

증거는 `reports/kpass-document-20261017/`의 `reparse-state.json`, `document-checks.json`, `tests.xml`, `full-tests.xml`, `runtime-smoke.json`에 있다.

다음 단계는 참조 문서 2개의 공식 다운로드 경로 확인·수집, 상세 안내와 연결 문서의 대조, 필요한 혜택 규칙 추출이다. 교통 혜택을 매장 할인 규칙으로 임의 전환하지 않는다.
