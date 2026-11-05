# 공식 브랜드 매장 대조 및 시드 보호

공식 매장 4개 브랜드·6개 출처의 정기 수집 연결 현황은 [STORE_DIRECTORY_SCHEDULER.md](STORE_DIRECTORY_SCHEDULER.md)에 기록했다.

2026-10-18 메가MGC커피 수집기와 후속 실측 결과는 [MEGA_STORE_COLLECTION.md](MEGA_STORE_COLLECTION.md)에 기록했다.

파리바게뜨 수집기 준비와 실제 조회 미완료 범위는 [PARIS_STORE_COLLECTION.md](PARIS_STORE_COLLECTION.md)에 기록했다.

2026-10-20 아웃백 공식 수집기와 실측한 좌표 불일치 보류 결과는 [OUTBACK_STORE_COLLECTION.md](OUTBACK_STORE_COLLECTION.md)에 기록했다.

2026-10-17 로컬 검증 기록. 주소 미확인 매장 32곳 중 스타벅스 후보를 공식 매장 검색과 대조했다. 이번 작업에서 실제 매장 DB의 이름·주소·혜택을 변경하지 않았다.

## 시드 재실행 시 검증 데이터 보존

`app/scripts/seed_stores.py`의 두 가지 덮어쓰기 문제를 수정했다.

- `MapLocation` 또는 `address_review` 감사 기록이 있는 기존 매장은 주소와 좌표를 보존한다. 검증 유효기간이 지났더라도 옛 시드 주소로 되돌리지 않는다. 검증의 현재 유효성은 기존 지도 검증 로직에서 판단한다.
- 기존 `StoreOffer.is_sample=False`인 혜택은 시드와 할인율이 같아도 변경하지 않는다. 이전 코드는 할인율이 같으면 실제 조건을 예시 조건으로 덮고 `is_sample=True`로 바꿀 수 있었다.
- 아직 검토하지 않은 매장 후보와 기존 예시 혜택의 갱신 동작은 유지한다.

실제 DB에 시드를 실행하지 않고 격리된 테스트 DB에서 확인했다. 관련 테스트 **73개 통과**, 실패·오류 0개.

테스트 파일: `tests/test_store_seed_preservation.py`, `tests/test_store_address_completion.py`, `tests/test_map_address_review.py`, `tests/test_map_location_matching.py`, `tests/test_stores.py`, `tests/test_map_sync.py`.

결과: `reports/store-brand-review-20261017/tests.xml`.

## 공식 스타벅스 조회 결과

공식 [매장 검색](https://www.starbucks.co.kr/store/store_map.do)의 공개 검색 화면에서 `회기`, `경희대`를 각각 조회했다. robots 정책을 먼저 확인하고, 로그인 없이 화면에서 발생하는 매장 검색 응답을 보관했다. 아래 시각은 UTC이며 JSON 파일은 응답을 JSON으로 다시 직렬화한 자료다.

| 조회어 | 관찰 시각 | 보관 파일 |
| --- | --- | --- |
| 회기 | 2026-10-17T12:13:28.371828+00:00 | `official-search-0.json` |
| 경희대 | 2026-10-17T12:13:38.567015+00:00 | `official-search-1.json` |

회기 검색 결과 중 동대문구 매장:

| 공식 코드 | 공식 매장명 | 공식 도로명 주소 |
| --- | --- | --- |
| 3391 | 경희대삼거리 | 서울특별시 동대문구 회기로 141 (회기동) |
| 4645 | 경희의료원 | 서울특별시 동대문구 경희대로 23 (회기동) |
| 9551 | 경희대 | 서울특별시 동대문구 경희대로 16-1 (회기동) |
| 9967 | 회기역사거리 | 서울특별시 동대문구 이문로 37 (회기동) |

기존 DB 36번 `경희대점`은 이미 검토한 공식 코드 9551과 같다. 그러나 미확인 DB 37번 `회기역점`의 주소는 `회기로 118`이며 공식 코드 9967의 이름·주소와 다르다. 검색 결과가 비슷한 지역에 있다는 이유로 37번을 `회기역사거리점`으로 바꾸거나 혜택을 승계하지 않는다. 두 검색 결과만으로 기존 후보의 폐점 여부도 확정할 수 없다.

증거: `reports/store-brand-review-20261017/`의 `capture_starbucks.py`, `starbucks-searches.json`, `official-search-0.json`, `official-search-1.json`. 검색별 관찰 시각과 보관 JSON의 SHA-256은 `starbucks-searches.json`에 기록했다.

## 다음 반영 조건

1. 공식 코드 9967의 매장명·주소를 카카오 장소와 별도로 대조한다.
2. 새 매장을 등록한다면 기존 `회기역점`과 별개의 매장으로 처리하고, 공식 코드와 출처를 감사 기록에 남긴다. 기존 후보를 자동 병합하지 않는다.
3. 나머지 브랜드도 공식 지점 식별자와 지점별 주소를 확보한 뒤 교정한다. 생협 사무실 주소나 대학 대표 주소는 개별 매장의 주소로 사용하지 않는다.
4. 장소 확인 뒤에도 카드사의 해당 가맹점 분류와 혜택 조건은 별도 검증한다. 공식 매장 검색 결과만으로 카드 할인 적용을 승인하지 않는다.

위 시점의 미확인 32곳은 계속 보류다. 후속 작업에서 별도로 검증된 매장 1곳을 아래와 같이 추가했다. 운영 배포는 하지 않았다.

## 후속 구현: 공식 매장과 카카오 장소 교차 확인

`app/scripts/discover_starbucks_store.py`를 추가했다. 기본 동작은 조회만 수행하며 `--apply`일 때만 로컬 SQLite에 등록한다.

- 공식 검색 응답의 SHA-256, 관찰 시각(7일 이내), 공식 코드와 매장명이 일치해야 한다. 이번 수집 범위는 서울 동대문구다.
- 기존 카카오 SDK 조회 기능을 공유한다. 공식 이름에서 명시적으로 구성한 지점명과 주소가 유일하게 일치해야 하며, 장소 URL·ID·좌표도 검사한다. 공식 좌표와 위도 또는 경도가 0.002도 넘게 다르면 보류한다. 이는 서울 부근에서의 보수적인 좌표 차이 검사이며 거리 계산 결과를 뜻하지 않는다.
- 적용 전에 SQLite 백업을 생성한다. 기존 지점 주소 교체·이름 변경·장소 ID 재연결은 자동 수행하지 않는다. 공식 코드 또는 카카오 장소 ID가 다른 매장에 이미 연결돼 있으면 거부한다.
- 등록 시 `official_store_discovery`와 `location_match` 감사 기록, `MapLocation`을 남긴다. 같은 자료를 재적용하면 기존 행과 관찰 시각을 보존한다.
- 공식 수집 기록은 로컬 운영자가 확보한 자료다. SHA-256은 보관 파일의 일관성을 검사하며 외부 제공자의 서명이나 수집 사실 인증을 뜻하지 않는다.
- 결제 혜택을 생성하거나 카드사의 가맹점 분류를 승인하지 않는다.

실행 예:

```powershell
.venv\Scripts\python.exe -X utf8 -m app.scripts.discover_starbucks_store `
  --manifest reports/store-brand-review-20261017/starbucks-searches.json `
  --query 회기 --official-id 9967 --official-name 회기역사거리 `
  --output reports/store-discovery-20261017/preview.json
```

같은 인자에 `--apply`를 추가하면 검증을 다시 수행한 뒤 등록한다.

### 실제 로컬 결과

- 공식 코드 **9967**, 카카오 장소 **24868732**가 이름·주소로 유일하게 일치했다.
- 신규 매장 **48번, 스타벅스 회기역사거리점**, 주소 `서울특별시 동대문구 이문로 37 (회기동)`으로 등록했다.
- 카카오 좌표 `37.5909755140802, 127.0564052274`를 지도 좌표로 사용했다. 공식 좌표와 약 35m 차이다.
- 기존 **37번 회기역점 / 회기로 118**은 변경하지 않았다. 미확인 기존 후보 32곳도 그대로다.
- 전체 매장 **48곳**, 매장 결제 혜택 **105건**. 이번 등록으로 추가된 결제 혜택은 **0건**이다.
- 로컬 `GET /stores/nearby`를 반경 100m, `verified_locations_only=true`로 조회해 HTTP 200과 신규 매장의 `location_verified=true`를 확인했다. 신규 매장에 `best_deal`은 없고 할인율도 확정하지 않는다.
- 재적용 검사 결과 `unchanged`이며 감사 기록도 늘지 않았다.
- 관련 테스트 **90개 통과**. 이름·주소 불일치, 중복 장소, 오류 응답, 잘못된 출처·좌표, 오래된 수집 자료, 기존 지점 충돌, 공식 코드·장소 재연결 방지를 포함한다.

자료: `reports/store-discovery-20261017/preview.json`, `applied.json`, `runtime-smoke.json`, `tests.xml`. 백업 경로는 `applied.json`에 기록했다. 이번 변경은 CLI와 로컬 DB에 반영했으며 서버 재시작이나 운영 배포는 하지 않았다.
