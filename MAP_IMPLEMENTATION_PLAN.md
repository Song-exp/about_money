# 할인 지도 데이터 연결 및 갱신

> 후속 보완계획(2026-10-13): [MAP_IMPROVEMENT_PLAN.md](MAP_IMPROVEMENT_PLAN.md). 추천 정확성·검증 근거·주변 검색·조건 계산·공식 출처 재검증의 남은 작업과 완료 기준은 해당 문서를 따른다. 아래 내용은 기존 구현 및 검증 이력이다.

> 2026-10-13 현재 구현·실측: [통합 보고서](reports/map-improvement-20261013/REPORT.md). 과거 주소 검색 첫 결과를 표시하던 방식은 폐기했고, 원문 승인·조건 판정과 위치 확인 기록을 사용한다. 실제 위치 대조는 `resolve_map_locations`로 수행하며 이전 `MAP_E2E_LIVE`의 22건 표시 주장은 현재 기준에 적용하지 않는다.

## 이번에 연결한 범위

- 거지맵 공개 동대문구 목록: https://xn--v69ak0xskm.com/restaurants/seoul/dongdaemun/
- 2026-10-11 실제 수집: 식당 22건. 이름, 주소, 대표 메뉴, 등록 가격, 개별 원문 링크를 DB에 저장했다.
- 등록 메뉴 가격은 `menu_price`로 분리한다. 할인율이나 절약액으로 계산하지 않는다. 제보 가격이며 매장 방문 전 확인이 필요하다.
- 공개 목록에 좌표와 가격 제보 일시가 없어, 좌표는 기존 카카오 주소 검색을 사용한다. 마지막 수집 시각은 가격 확정 시각이 아니다.
- 지도에서 거지맵 식당 표시를 별도로 켜고 끌 수 있다. 최초에는 모든 업종을 표시한다.
- 동대문구 외 지역과 실제 매장 가격 검증은 이번 수집 범위에 포함되지 않는다.

## 동작하는 파이프라인

`공개 목록/등록된 HTTPS 피드 → robots 확인 → 크기 제한 → 구조·주소·가격·기간 검증 → 출처별 upsert → 상태 API → 지도 갱신`

- `MAP_SYNC_ENABLED=true`, 기본 300초마다 수집한다. 장학금 스케줄 설정과 독립적으로 실행한다.
- ETag/Last-Modified 조건부 요청, 실패 시 이전 정상 자료 보존, 지수 재시도, 중복 실행 방지.
- 공개 목록 전체 건수와 실제 파싱 건수가 다르거나 양식이 바뀌면 갱신을 중단한다.
- 명시적인 전체 스냅샷에서 누락된 항목만 해당 출처의 활성 상태를 해제한다. 다른 출처 데이터는 건드리지 않는다.
- 기간이 지난 혜택은 API에서 제외하고, 예시 할인과 종료 제보가 누적된 할인은 추천율에서 제외한다.
- 지도는 30초마다 출처 상태를 확인하고 새 수집 시각을 발견하면 데이터를 다시 불러온다.
- 개발 서버 재시작 후 자동 갱신이 실행된다. 운영 서버는 새 마이그레이션 적용과 배포가 필요하다. 여러 프로세스로 운영할 때는 스케줄러를 하나의 전용 worker에서 실행해야 한다. 현재 중복 방지 락은 프로세스 내 범위다.

## 실제 프로모션 제공처 연결: 남은 외부 의존성

현재 `MAP_PROMOTION_SOURCES_JSON=[]`이므로 실제 프로모션은 수집하지 않는다. CODEF 보유 카드 조회는 프로모션 제공 피드와 별개의 작업이다.

### CODEF 보유 카드 연결 (2026-10-12)

- [x] 환경변수 인증키 및 실제 OAuth 토큰 발급 확인
- [x] 카드사 아이디/공동인증서 화면 → 계정 생성·수정 → 보유 카드 조회
- [x] 현대·KB·하나 추가 입력 및 우리 본인확인 정보, 씨티 SMS 2-way 흐름
- [x] 암호화된 연결 식별자, 계정별 접근 제어, 요청·응답의 민감 정보 제거
- [x] 카드 DB 매칭 확인 → 계정 저장 → 새로고침 시 보유 카드 복원
- [x] 재조회·연결 해제·탈퇴 시 원격 인증수단 해제
- [x] 전체 회귀 1,265개, 카드 연결 브라우저 2개, 프로필 브라우저 4개 및 빌드 통과
- [ ] 사용자 본인의 카드사 인증으로 실제 계정 생성·보유 카드 조회 검증
- [ ] 정식 서비스 요금 및 운영 배포

실제 키 인증은 통과했다. 카드 조회와 SMS 브라우저 검증은 제공자 응답을 모의 처리한 결과이며, 실제 금융기관 연결 성공을 의미하지 않는다. 구현·검증 기록: `reports/codef-integration-20261012/REPORT.md`.

공식·제휴 API/피드의 URL, 인증 방식, 사용 범위가 확인되면 제공처별 어댑터를 연결해야 한다. 현재 수신 계약은 매장별 간편결제 정률 할인이다. 정액 할인, 카드 혜택, 전월 실적 및 월 한도 계산은 별도 모델 확장이 필요하다.

피드 설정 예시(실제 연결된 제공처가 아님):

```dotenv
MAP_PROMOTION_SOURCES_JSON=[{"key":"partner","url":"https://provider.example/promotions.json"}]
```

등록된 출처에 대해 `POST /admin/crawl/map-sources/promotions/promotion:partner`로 같은 JSON을 전송하면 즉시 DB 반영한다. 기존 `X-Admin-Token` 인증을 사용한다. 제휴사에는 이 토큰을 직접 공유하지 않고 내부 수신 어댑터가 호출하도록 구성한다.

```json
{
  "schema_version": 1,
  "complete": true,
  "items": [{
    "id": "event-1", "brand": "식당명", "branch": "지점명",
    "address": "서울특별시 동대문구 ...", "category": "restaurant",
    "lat": 37.59, "lng": 127.05, "pay_method": "kakao",
    "discount_rate": 10, "condition_text": "제공처가 명시한 적용 조건",
    "valid_from": "2026-10-11", "valid_to": "2026-10-31",
    "source_url": "https://provider.example/event-1"
  }]
}
```

`complete=true`는 전체 목록이며 누락된 기존 항목을 비활성화한다. `false`는 부분 변경이다. 원문 링크는 등록 피드와 동일한 도메인만 허용한다. URL에 인증 쿼리나 비밀키를 넣는 피드는 현재 지원하지 않는다. 필요한 경우 환경변수 기반 인증 헤더 어댑터를 추가한다.

5분 주기 수집은 준실시간이다. 제공처 웹훅 또는 이벤트 스트림이 있어야 실제 즉시 갱신을 보장할 수 있다. DB 반영 후 열린 지도 화면은 최대 30초 뒤 반영한다.

## 실행 및 확인

```powershell
.venv\Scripts\python.exe -m app.scripts.sync_map_sources --output reports/map-sync.json
.venv\Scripts\python.exe -m pytest tests/test_map_sync.py tests/test_stores.py tests/test_env_contract.py -q
```

수동 실행은 SQLite를 `data/backups/`에 백업한 뒤 갱신한다. 운영 DB에는 `alembic upgrade head`를 적용한다.

- 공개 상태: `GET /stores/source-status`
- 관리자 강제 수집: `POST /admin/crawl/map-sources/sync`
- 실제 수집 결과: `reports/map-integration-20261011/sync.json`

## 검증 결과와 지도 외부 설정

- 전체 회귀 테스트 1,239건 통과. 이후 추가한 프로모션 출처·신선도 및 만료 필터를 포함한 관련 테스트 26건 통과.
- 프런트엔드 TypeScript 및 배포 빌드 통과. 기존 dist 정리 접근 문제를 피해 `npm run build -- --emptyOutDir false`로 검증했다.
- 최초 SDK 검사에서는 HTTP 401 도메인 미등록 오류가 있었다. 사용자 도메인 등록 후 재검증에서 SDK 및 22건 주소 검색이 HTTP 200으로 정상 응답했다. 배포 주소도 같은 JavaScript 키에 등록해야 한다.
- 현재 결과: `reports/map-integration-20261011/browser/verification-live.json`, `geojimap-live.png`. 기존 `sdk-response.json`과 `diagnostics.json`은 등록 전/재검증 중의 실패 진단 이력이며 현재 상태를 나타내지 않는다. 키는 진단 출력에 포함하지 않는다.
- 별도의 브라우저 계약 검사는 합성 좌표로 실제 HTTP API, 22건 표시, 가격·원문 링크, 토글을 검증한다. 이 검사는 실제 식당 좌표 정확도를 증명하지 않는다.
- 브라우저 계약 검사 및 실제 SDK 검사 총 2건 통과. 실제 주소 검색으로 식당 22건이 주변 결과에 포함됐으며 가격·원문 링크·표시 토글도 검증했다. 카카오가 화면 밖 오버레이를 DOM에서 제외하므로, 전체 22건과 현재 확대 범위의 마커 4개를 구분해서 확인했다. 좌표 검색 성공은 매장 위치를 현장에서 확인했다는 의미는 아니다.
- 실제 SDK 재검증은 `MAP_E2E_LIVE=true`를 설정한 뒤 `python -m pytest tests/browser_e2e_map.py`로 실행한다.
- 실제 화면에서 발견한 메뉴 가격 식당의 `최대 0%` 추천 표시를 제거했다. 양수 할인율이 있는 항목만 할인 추천 대상으로 삼는다.
