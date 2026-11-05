# Deep Oil 공식 안내장 수집·교차 검증

2026-10-16: 공식 공시실 검색 → 새 다운로드 링크 발급 → PDF 원문 보관 → PDFium 텍스트 복원 → 상품 페이지와 대조 → 관리자 검토까지 연결했다. 운영 배포나 Deep Oil 게시 승인은 하지 않았다.

## 수집 방식

공식 출처는 [신한카드 상품공시실 약관 목록](https://www.shinhancard.com/mob/MOBFM12051N/MOBFM12051R01.shc)이다. 공개 화면의 검색과 동일하게 `mbw_json` 폼으로 `Deep Oil`을 검색한다. 정확한 상품명 1건과 검색 완료 여부, 문서 식별자·게시일 형식을 검증한다. 제휴 변형 상품이나 동명 중복, 미완료 목록은 자동 선택하지 않는다.

안내장 다운로드 링크의 `aFilenm`은 만료되므로 DB 출처 식별자로 저장하지 않는다. 같은 HTTP 세션에서 매번 검색한 링크로 내려받으며, DB에는 고정 공시실 URL과 SHA-256 기반 PDF 스냅샷을 보관한다. 다운로드 만료 오류의 HTML이 HTTP 200으로 반환되어도 PDF 수집 성공으로 인정하지 않는다. 리다이렉트·robots 거부·응답 크기 초과도 실패 처리한다.

`app/services/shinhan_disclosure.py`를 기존 `collect_due`에 연결했다. 기존 실행 잠금·호스트 요청 간격·실패 백오프를 사용한다. `bootstrap_supporting_documents`가 Day와 Deep Oil의 출처를 함께 등록하며, Deep Oil은 주 1회 갱신한다. 5분 간격 워커는 확인 시각이 된 출처만 처리한다. 이 단계는 전체 신한 상품의 안내장 수집 지원을 뜻하지 않는다.

원본 PDF의 기존 추출 텍스트는 덮어쓰지 않는다. `CardDocumentText`에 PDFium 파생 텍스트와 원문·텍스트 지문을 별도로 저장한다. 재처리는 같은 원문·파서에 대해 중복 생성하지 않는다. 문서 대조는 최신 관찰 스냅샷, 발급사, 7일 이내 전체 HTTP 200 수집, 파생 텍스트 지문을 확인한다.

## 실제 대조 결과

실제 공식 PDF 586,328바이트·2페이지를 확보했다. 카드사 검색 결과의 게시일은 2026.08.26이었다. 게시일·심의일을 혜택 시행일이나 만료일로 전용하지 않는다.

공식 상품 페이지는 [Deep Oil 상세 안내](https://www.shinhancard.com/pconts/html/card/apply/credit/1188274_2207.html)다. `compare_deep_oil`은 생활서비스 브랜드·할인율·이용금액 한도·한도 단위·가족 및 택시 공유, 높은 실적 구간 문구, 범위 밖 영화관 안내까지 10개 항목을 대조한다.

실제 2개 불일치를 저장했다.

- `spend.higher_label`: 상품 페이지는 `70만원 이상`, PDF 표는 `70만원 미만 이상`이다. PDF 페이지를 렌더링해 육안으로도 해당 문구를 확인했다. 임의로 문구를 교정하지 않는다.
- `outside_scope.cinema.brand`: 상품 페이지는 롯데시네마, PDF는 CGV다. 영화는 현재 추출한 지도 규칙 범위 밖임을 함께 표시한다. 생활서비스 브랜드 불일치로 오인하지 않도록 범위를 구분한다.

채널·원 단위·잔여 이용금액을 넘는 거래의 부분 적용 정책은 여전히 미해결이다. 일치한 필드가 있어도 전체 약관 검토 완료로 보지 않는다. 새 문서가 없는 경우도 이제 `required=true`로 표시하며, 누락·만료·위변조·파싱 실패·문서 충돌은 승인을 차단한다.

## 검증과 로컬 반영

- 관련 테스트 144개 통과. 전체 테스트 1,577개 통과, 실패·건너뜀 없음.
- 관리자 실제 HTML/API 및 기존 카드 등록 흐름의 브라우저 검사 4개 통과. Deep Oil의 두 문서 충돌 표시와 승인 차단을 확인했다.
- 기존 파이프라인의 실제 실행에서 신규 출처 39번 PDF 1건 저장, 수집 실패 0건, 자동 승인 0건. 상품 후보·보유 카드 연결은 추가하지 않았다.
- 로컬 서버를 새 코드로 재시작하고 예약 수집 재개 및 실제 버전 7 API 응답을 확인했다. `supporting_document_conflict`, 대조 10항목·불일치 2건·승인 불가를 반환했다.

증거는 `reports/card-deep-oil-crosscheck-20261015/`에 보관한다. 전날 시작한 조사의 디렉터리를 이어 사용했다. `collection-state.json`, `document-checks.json`, `full-tests.xml`, `browser-tests.xml`, `runtime-smoke.json`, `leaflet-last.png`를 확인하면 된다.

```powershell
.venv\Scripts\python.exe -X utf8 -m app.scripts.collect_card_sources --register-documents --collect-due --output reports/card-disclosure/collection.json
.venv\Scripts\python.exe -X utf8 -m app.scripts.collect_card_sources --reparse-documents --document-review-output reports/card-disclosure/checks.json --output reports/card-disclosure/state.json
```

`--collect-due`는 Deep Oil만 강제로 수집하는 명령이 아니라 기존 전체 출처 중 확인 시각이 된 출처를 수집한다. `--reparse-documents`는 보관 파일로 대조하며 외부 요청을 하지 않는다.

다음에는 실적 문구와 공식 문서 적용 관계, 채널·원 단위·한도 경계 정책을 확인해야 한다. 명시적 근거 확보 전에는 실제 할인액이나 지도 게시를 활성화하지 않는다.
