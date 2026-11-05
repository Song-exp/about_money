# K-패스 연결 문서 경로와 수집 제한

2026-10-17: K-패스의 상품안내서·개인회원 약관 식별자를 출처 수집 및 상태 조회에 연결했다. **두 PDF는 아직 확보하지 못했고, 내용 교차 검증도 완료하지 않았다.**

## 경로 확인과 실제 제한

[공식 K-패스 페이지](https://www.tossbank.com/card/k-pass)의 원본 안내 데이터에 상품안내서 `60303005`, 개인회원 약관 `60303001`이 있다. 새 비로그인 브라우저에서 상품안내서 링크를 눌렀을 때 다음 공개 다운로드 요청이 발생하는 것을 확인했다.

`https://api.tossbank.com/api-public/disclosure/attachment/document/document-no/60303005`

회원약관은 원문 문서 식별자와 같은 다운로드 경로 형식으로 `60303001`을 연결했다. 파일 본문을 받아 실제 문서 정체·개정일을 확인한 상태는 아니다.

두 경로의 자동 수집 사전 검사에서 `source_disallows_fetch`가 반환됐다. 해당 API의 robots 정책이 거부하므로 실제 PDF 요청은 진행하지 않았다. 브라우저로 우회 다운로드하거나 오래된 공지의 다른 파일을 현재 약관으로 대체하지 않았다.

## 구현

- `card_linked_documents.py`에 문서 식별자·역할·경로를 등록했다. `api.tossbank.com`에서는 이 두 경로만 허용하며 다른 API 경로·문서 번호·쿼리는 거부한다.
- 기존 문서 등록 명령이 Day·Deep Oil 안내장과 함께 두 출처를 등록한다. 상품 후보나 보유 카드를 추가하지 않는다.
- 정상 수집 정책이 허용될 경우 기존 원문 아카이브·PDFium 파생 텍스트 경로로 처리한다. 등록 주기는 주 1회이며 정책 거부 상태에서는 기존 24시간 재확인 대기를 유지한다.
- K-패스 보관 HTML과 상세 텍스트의 지문을 다시 확인하고 원문 JSON의 실제 참조와 등록된 출처를 대응한다. 발급사·식별자·최신 관찰 스냅샷·본문 지문·7일 이내 전체 수집 여부를 확인한다.
- 참조가 바뀌거나 지원하지 않는 문서 번호면 URL을 추측해 수집하지 않고 확인 대기로 남긴다.
- `/admin/map/card-sources/7/document`의 `linked_documents`에서 문서별 상태를 확인한다. 수집이 성공해도 `ready_for_crosscheck`까지만 표시하고, 교차 검증·승인은 완료로 바꾸지 않는다.

## 실제 적용 결과

로컬 출처 40번(상품안내서), 41번(회원약관)을 등록했다. 실제 파이프라인 실행 결과 수집 0·정책 거부 2·자동 승인 0건이다. 출처 수는 41개가 됐다. 두 문서는 API에서 `collection_blocked`, `crosscheck_completed=false`, `public_approval=false`로 반환한다. 기존 K-패스 상세 텍스트 5,462자는 유지된다.

관련 테스트 73개, 전체 테스트 **1,646개**가 통과했다. 관리자 실제 API에서도 두 제한 상태와 운영 알림의 정책 분류를 확인했다. 로컬 서버와 예약 작업을 새 코드로 실행했다. 프런트엔드 변경·운영 배포는 하지 않았다.

검증 자료: `reports/kpass-linked-documents-20261017/link-probe.json`, `document-probe.json`, `collection-result.json`, `runtime-smoke.json`, `tests.xml`, `full-tests.xml`.

## 다음 진행에 필요한 자료

수집이 허용되는 공식 경로나 사용자가 공식 화면에서 직접 내려받아 제공한 PDF가 필요하다. 파일을 확보하면 문서명·식별자·개정일·적용 대상을 확인하고 원문 지문을 기록한 뒤 상세 안내와 대조해야 한다. 제공받은 파일을 자동 HTTP 수집 성공으로 기록하거나, 수집일을 혜택 시행일로 사용해서는 안 된다.

## 수동 PDF 반입 기능 (2026-10-17)

`app.scripts.import_card_document`로 네트워크 요청 없이 로컬 PDF를 반입할 수 있다. 등록된 두 문서 번호와 공식 원본 페이지/다운로드 URL만 받는다. 최대 20 MiB, 100페이지의 읽을 수 있는 PDF가 대상이며, 텍스트 복원이 불가능하면 반입 기록을 남기지 않는다.

로컬 SQLite에서는 먼저 `python -m app.scripts.prepare_card_collection`로 백업과 추가 테이블 생성을 수행한다. 배포 DB에는 Alembic 마이그레이션 `4def62509873`을 적용한다.

```powershell
.venv\Scripts\python.exe -X utf8 -m app.scripts.import_card_document `
  --document-id 60303005 `
  --file 'data/manual/kpass-product-guide.pdf' `
  --title '토스뱅크 체크카드 상품안내서' `
  --revision 'unknown' `
  --obtained-at '2026-10-17T09:00:00+09:00' `
  --operator 'local-reviewer' `
  --origin-url 'https://www.tossbank.com/card/k-pass'
```

확보 시각은 실제 값으로 입력하고 개정일을 모르면 `unknown`으로 명시한다. 문서명과 개정일은 반입자의 선언이며 검증 결과가 아니다. 파일명·원본 경로는 저장하지 않고 기존 콘텐츠 해시 기반 원문 보관소와 PDFium 텍스트에 연결한다. 반입 기록은 별도 `card_document_imports` 테이블에 보관한다. 같은 문서 원본을 재반입해도 최초 기록을 덮어쓰지 않는다.

수동 반입은 HTTP 성공 시각·재시도 일정·정책 차단 상태·수집 이력을 갱신하지 않는다. 관리자 문서 상태의 `manual_document`에 별도로 표시하며, 원문·텍스트 지문이 맞으면 `manual_identity_review_required`, 변조/분실이면 `integrity_failure`다. 원래 `collection_blocked`와 교차 검증·공개 승인 대기는 그대로 유지한다.

실제 PDF 두 개는 아직 확보하지 않았다. 다음 작업은 실제 문서를 반입한 뒤 식별자·개정일·적용 대상과 상세 안내의 조항을 대조하는 것이다.

검증: 전체 회귀 테스트 1,654개 통과 후 복원 실패 롤백 테스트를 추가하여 반입 전용 테스트 9개를 다시 통과했다. 로컬 DB는 백업 후 추가 테이블을 생성했고, 기존 카드 7개·혜택 52개·매장 제안 105개는 유지됐다. 서버 재시작 후 예약 수집 실행도 확인했다. 결과는 `reports/manual-card-document-20261017/`에 보관한다. 운영 배포는 하지 않았다.

## HTTP 캐시와 원문 무결성 보완 (2026-10-17 후속)

수집기가 가장 큰 스냅샷 ID를 HTTP 캐시로 사용하는 경로를 수정했다. 현재는 마지막 정상 HTTP 관찰 기록을 우선 사용하므로, 공고가 A→B→A로 되돌아간 후 304를 받아도 A에 연결된다. 관찰 기록이 없는 이전 아카이브를 사용할 때는 수동 반입 스냅샷을 제외한다. 자동 수집 원본이 없으면 전체 본문 수집을 강제하며 304만으로 수동 파일을 수집 성공으로 기록하지 않는다.

연결 문서의 `ready_for_crosscheck` 판정에는 실제 보관 원문의 SHA-256 검증을 추가했다. 원본 누락·변조·보관소 밖 경로는 `document_archive_integrity_failure`로 남긴다. 상위 K-패스 문서와 파생 텍스트의 스냅샷 연결도 확인한다. 이 판정은 문서 정체·개정일·적용 대상의 검증이나 공개 승인 완료를 뜻하지 않는다.

수집·탐색·복구·반입·문서 검증 관련 테스트 84개가 통과했다. 검증 자료: `reports/card-cache-integrity-20261017/tests.xml`. 실제 PDF 확보와 조항 대조는 여전히 대기 중이다.

## 수동 문서 검토 자료 생성 (2026-10-17 후속)

반입 기록을 기준으로 검토용 JSON을 생성한다. 추가 DB 기록·수집 요청·승인 변경 없이 PDF 원문과 파생 텍스트의 지문, 반입자가 선언한 메타데이터, 최신 관찰 K-패스 공고의 텍스트·인용 위치·문서 참조를 함께 제공한다. PDF 확보 전에는 실제 검토 자료를 생성하거나 가상 반입 기록을 운영 DB에 넣지 않는다.

```powershell
.venv\Scripts\python.exe -X utf8 -m app.scripts.review_card_document `
  --import-id 1 `
  --output 'reports/kpass-review/import-1.json'
```

`--import-id`는 실제 반입 명령이 반환한 번호로 지정한다. 관리자 API는 `GET /admin/map/card-document-imports/{import_id}/review-pack`이며 기존 관리자 인증 정책을 따른다. 토큰 설정 시 `X-Admin-Token` 인증을 요구하고, 개발 모드의 빈 토큰 설정에서는 기존 정책대로 인증을 생략한다. 운영 모드는 빈 토큰을 허용하지 않는다. 기존 연결 문서 상태의 `manual_document.review_pack_url`에서도 경로를 확인할 수 있다. 검토 JSON에는 반입자 식별값과 전체 텍스트가 있으므로 내부 검토 자료로 취급한다.

검토 자료의 `review_evidence_ready=true`는 파일과 텍스트의 무결성·발급사 연결·현재 공고의 문서 참조·최근 7일 내 전체 본문 관찰이 확인됐다는 뜻이다. 메타데이터의 진위나 혜택 조항 일치를 확인했다는 뜻은 아니다. 원본 분실/변조·본문 변경·수집 오류·과거 공고·문서 참조 변경·미래 반입 시각은 `issues`에 남고 준비 상태는 false가 된다. 파일 확보일은 혜택 시행일로 사용하지 않는다.

문서 정체, 개정·시행 기간, K-패스 적용 대상, 조항별 비교, 혜택 규칙 추출을 `required_checks`로 명시한다. `metadata_verified`, `crosscheck_completed`, `public_approval`는 계속 false다. 관련 테스트 49개 통과. 결과: `reports/card-document-review-20261017/tests.xml`.

로컬 서버에 반영 후 OpenAPI 경로 등록, 없는 반입 기록의 404 응답, 기존 문서 조회 200 응답을 확인했다. 토큰 설정 환경의 인증 거부는 격리 테스트에서 검증했다. 실제 PDF가 없어 로컬 운영 DB에는 반입 기록과 실제 검토 자료를 생성하지 않았다. 런타임 확인 결과는 같은 폴더의 `runtime-smoke.json`에 보관한다.

## 실제 공식 문서 확보 재점검 (2026-10-17)

공식 홈페이지의 상품공시실 링크는 `https://www.tossbank.com/customer/product-disclosure`다. 공식 공시실과 `https://www.tossbank.com/notice/594`를 기존 robots 검사 경로로 읽고 HTML 및 내장 JSON의 PDF 참조를 확인했다. 두 페이지의 아카이브와 SHA-256은 `reports/card-document-access-20261017/official-page-probe.json`에 보관했다. 첨부 다운로드 요청은 0건이다.

공시실 HTML에서는 직접 PDF 링크를 확인하지 못했다. 공지 594에는 상품안내서 변경대비표(첨부 식별자 422)와 상품안내서(423)가 있으며 파일명은 각각 `토스뱅크 체크카드 상품안내서_변경대비표_250806개정.pdf`, `토스뱅크 체크카드 상품안내서_20250806개정.pdf`다. 첨부 식별자는 현재 K-패스 문서 번호 `60303005`/`60303001`과 다른 체계이므로 다운로드 URL을 추측하지 않았다. 2025년 개정본을 현재 K-패스 적용 약관으로 등록하지 않았다.

확인한 공식 페이지에서 현재 두 문서의 수집 가능한 대체 경로를 확보하지 못했다. 기존 차단 API의 재요청·정책 우회·혜택 승인·서버 코드 변경은 하지 않았다. 이 단계는 현재 공식 화면에서 직접 내려받은 상품안내서와 개인회원 약관 PDF가 제공되거나 수집 허용 경로가 확인되어야 이어갈 수 있다. 제공받으면 이미 구현한 반입→검토 자료 생성→실제 조항 대조 순으로 진행한다. 이번 변경은 조사 기록뿐이므로 테스트·서버 재시작은 반복하지 않았다.
