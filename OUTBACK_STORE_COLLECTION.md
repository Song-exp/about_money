# 아웃백 공식 매장 수집 및 좌표 대조

후속으로 지점명 검색 1개를 로컬 정기 수집 워커에 연결했다. 설정·실행 결과와 미완료 범위는 [STORE_DIRECTORY_SCHEDULER.md](STORE_DIRECTORY_SCHEDULER.md)에 기록했다.

2026-10-20. 공식 원문 수집·파서·카카오 장소 대조를 실행했다. **좌표 불일치로 신규 등록은 보류**했다.

## 구현

- `app/scripts/probe_outback_stores.py`: robots 정책 확인 후 [공식 모바일 매장 찾기](https://m.outback.co.kr/store/findStore.do)를 1회 수집한다. TLS 검증과 30초 제한 시간을 유지하고, 요청 간격 2초·응답 크기 2MB 제한을 적용한다.
- `app/services/outback_store_identity.py`: 페이지의 `storeJsonString`을 실행하지 않고 문자열 리터럴과 JSON으로 읽는다. ID·이름·도로명 주소·상세 주소·좌표·배달 전용 여부를 같은 공식 항목에서 추출한다. 중복 ID, 불명확한 데이터 구조, 비정상 좌표를 거부한다.
- `app/scripts/discover_outback_store.py`: 7일 이내 관찰 시각·보관 원문 해시·정확한 공식 ID/지점명을 확인하고 기존 카카오 대조 절차로 연결한다. 현재 후보 범위는 서울 동대문구이며 배달 전용 매장은 제외한다. 아웃백 브랜드의 지도 표시 이름은 `아웃백스테이크하우스 + 공식 지점명`으로 명시적으로 구성한다.
- 등록을 통과하는 아웃백 매장은 `restaurant`로 분류한다. 기존 이름·주소를 덮어쓰거나 카드 혜택을 생성하지 않는다. 기본은 조회이며 `--apply`도 검증을 통과할 때만 백업 후 등록한다.

페이지에 포함된 항목 **103개**를 파싱했다. 이는 해당 응답의 항목 수이며 전국 전체 매장 수 또는 수집 완전성을 뜻하지 않는다(`complete=false`).

## 실제 대조 결과

| 항목 | 공식 페이지 | 카카오 장소 |
| --- | --- | --- |
| 식별자 | 공식 ID **279** | 장소 ID **158803290** |
| 지점명 | 청량리역사점 | 아웃백스테이크하우스 청량리역사점 |
| 도로명 주소 | 서울 동대문구 왕산로 214 | 서울 동대문구 왕산로 214 |
| 상세 주소 | 청량리역사 Connect Place 4층 | 장소 응답에 층 정보 없음 |
| 좌표 | 37.5841971, 127.051139 | 37.58056576973557, 127.04746952564622 |

이름·건물 주소로 유일한 일치 후보 1개를 찾았지만, 좌표 사이 거리는 약 **517.3m**다. 기존 좌표 차이 검사(위도 또는 경도 차이 0.002도 초과)가 이를 차단했다. 허용 폭을 넓히거나 어느 좌표가 맞는지 임의로 결정하지 않았다. 이 검사는 축별 좌표 차이 제한이며 0.002도가 고정된 미터 거리를 뜻하지 않는다.

실행 결과는 `status=pending`, `applied=false`, `reason=official_place_coordinates_disagree`다. `outback-applied.json`은 `--apply` 요청의 결과 파일명일 뿐 실제 반영이 됐다는 뜻이 아니다. 보류 전에 DB 백업·등록 분기로 진입하지 않았다.

기존 로컬 **42번 / 아웃백 청량리점 / 왕산로 214**는 변경하지 않았다. 현재 로컬 DB에서 **매장 50곳 / 결제 혜택 105건**을 확인했다. 신규 혜택·카드사 업종 확정·혜택 승인·운영 배포는 없다.

## 재현과 증거

```powershell
.venv\Scripts\python.exe -X utf8 -m app.scripts.probe_outback_stores `
  --output reports/store-brand-review-20261020/outback-capture

.venv\Scripts\python.exe -X utf8 -m app.scripts.discover_outback_store `
  --capture-dir reports/store-brand-review-20261020/outback-capture `
  --official-id 279 --official-name 청량리역사점 `
  --output reports/store-brand-review-20261020/outback-preview.json
```

실제 수집 자료: `reports/store-brand-review-20261020/outback-capture/`, `outback-applied.json`, `coordinate-review.json`.

관련 테스트 **176개 통과**. 공식 ID·지점명 불일치, 중복/변경된 데이터 구조, 잘못된 좌표, 오래된 자료·해시·출처, 배달 전용 매장, 다른 지역 배제, 음식점 분류와 혜택 미생성 및 기존 지도 검증을 확인했다. 결과는 같은 폴더 `tests.xml`이다.

등록 재검토에는 주소와 좌표를 함께 확인할 수 있는 독립적인 공식 건물·매장 위치 자료 또는 공식 좌표 수정 확인이 필요하다. 현재 정보만으로 기존 청량리점을 청량리역사점으로 병합하지 않는다. 수집·대조 CLI를 구현했으며 정기 스케줄러 연결과 운영 배포는 별도다.

## 후속: 공식 실시간 매장 검색 연결

공식 매장 찾기 페이지의 `getStoreJsonList` 동작을 확인하고 공개 `GET /store/storeJsonList.do` 검색을 수집기에 추가했다. 현재는 전국 데이터가 포함된 페이지를 반복 수집하는 대신 지점명 1개를 조회할 수 있다.

```powershell
.venv\Scripts\python.exe -X utf8 -m app.scripts.probe_outback_stores `
  --query 청량리 --output reports/outback-cheongnyangni-search

.venv\Scripts\python.exe -X utf8 -m app.scripts.discover_outback_store `
  --capture-dir reports/outback-cheongnyangni-search `
  --official-id 279 --official-name 청량리역사점 `
  --output reports/outback-cheongnyangni-preview.json
```

지점 검색은 공식 화면과 동일하게 빈 `sdCd`, `sggCd`와 명시적인 `searchKeyword`를 전송한다. 원문과 검색 URL·관찰 시각·해시를 보관하고, 검증 단계에서 호스트·경로·검색어·지역 필터가 바뀌지 않았는지 확인한다. 공식 화면에서 사용하는 성공 코드 **0005**와 `resultData.storeList` 구조를 검사한다. 오류 응답이나 구조가 바뀐 응답을 0건으로 취급하지 않는다. 성공 코드와 명시적인 빈 목록이 함께 있어야 0건으로 읽는다.

실제 `청량리` 조회에서는 **청량리역사점 1곳**이 반환됐다. 공식 ID 279·이름·전체 주소·좌표가 앞서 받은 공식 페이지 항목과 같았다. 따라서 단순한 정적 페이지 캐시 문제라고 판단할 근거는 없다. 같은 제공자의 두 응답이므로 독립적인 출처 2개로 세지 않는다.

기존 카카오 관찰 응답과의 좌표 차이는 여전히 **517.3m**이며 `official_place_coordinates_disagree`가 유지된다. 이번 대조에서는 카카오를 다시 조회하지 않았다. 각 관찰 시각은 별도로 남겼다.

- 공식 페이지 관찰: `2026-10-20T04:18:54.046087+00:00`
- 공식 지점명 검색 관찰: `2026-10-20T06:22:12.036889+00:00`
- 카카오 장소 관찰: `2026-10-20T04:21:19.504354+00:00`

롯데 공식 [청량리점 층별 안내](https://m.lotteshopping.com/store/floor/main?cstrCd=0004)도 살펴봤지만, 해당 공개 응답만으로 커넥트플레이스 아웃백 매장의 좌표를 확정할 수는 없었다. 건물 명칭·층 안내를 지점 좌표 확인으로 대체하지 않았다. 독립적인 정확한 위치 근거 확보는 여전히 미완료다.

관련 테스트 **188개 통과**. 검색 성공 코드·응답 구조·빈 목록 구분, 검색어/지역 필터/호스트 변경 차단을 추가 검증했다. 실제 보관 원문을 새 파서로 읽고 공식 페이지와 비교한 결과는 `reports/store-brand-review-20261020/source-crosscheck.json`, 검색 원문은 `outback-search/`, 테스트 결과는 `search-tests.xml`이다.

이번 후속 작업에서도 DB를 변경하지 않았으며, 실제 확인 집계는 **매장 50곳 / 결제 혜택 105건**이다. 기존 42번 후보는 유지한다. 지점 검색 CLI 연결을 완료했으며 좌표 문제 해결·정기 실행 연결·운영 배포는 완료되지 않았다.
