# 파리바게뜨 공식 매장 수집 준비

후속으로 동대문구 조회를 로컬 정기 수집 워커에 연결했다. 실제 예약 실행과 미완료 범위는 [STORE_DIRECTORY_SCHEDULER.md](STORE_DIRECTORY_SCHEDULER.md)의 후속 절에 기록했다.

현재 상태: **실제 검색 응답 2건 확보, 지점 파서와 등록 연결 구현 완료**. 서울 경희대 후보와 연결할 근거가 없어 카카오 대조와 DB 반영은 수행하지 않았다. 아래 준비 기록 이후의 실제 결과는 마지막 절에 기록했다.

## 공식 조회 경로 확인

기존 참고 주소 `https://company.paris.co.kr/store/`는 실제 접속에서 TLS 호스트명 불일치가 발생했다. 인증서 검증을 해제하지 않고 현재 공식 홈페이지의 [매장 찾기](https://www.paris.co.kr/store)를 확인했다.

공식 페이지의 공개 검색 코드는 `GET /wp-admin/admin-ajax.php`에 `action=pb_store_get_list`, `type=#search`, `keyword` 및 필터를 전달한다. 수집기는 이 읽기 전용 검색 동작만 사용한다.

확인한 robots.txt는 `/wp-admin/`을 차단하면서 `/wp-admin/admin-ajax.php`를 명시적으로 허용한다. 기존 기본 파서는 먼저 나온 차단 규칙을 적용했다. `app/services/robots_policy.py`를 추가해 단순 ASCII 경로 규칙에서 가장 구체적인 경로를 우선하도록 했다. 동률은 Allow를 우선하며 관리자 페이지 자체는 계속 차단한다. 이는 [RFC 9309 2.2.2](https://www.rfc-editor.org/rfc/rfc9309.html#section-2.2.2)의 경로 우선순위에 따른다.

이 도우미는 전체 RFC 구현이 아니다. 와일드카드·끝 앵커·인코딩된 규칙 등 지원 범위 밖 패턴이 있으면 검토가 필요하다는 오류로 중단한다. 다른 수집기의 robots 판정을 일괄 변경하지 않았다.

자료: `reports/store-brand-review-20261018/paris-search.html`, `paris-robots.txt`. 처음 매장 검색 실행은 중단됐으나 후속 실행에서 `paris-khu/`, `paris-hoegi/`의 실제 검색 응답을 확보했다. 기존 페이지 HTML을 특정 지점 검색 결과로 간주하지 않는다.

## 구현한 수집 동작

`app/scripts/probe_paris_stores.py`:

- 비어 있거나 지나치게 긴 검색어를 거부한다.
- 인증서 검증을 유지하고, 리다이렉트를 자동 추적하지 않는다.
- robots 조회 실패·차단이면 매장 검색을 수행하지 않는다.
- 공개 조회 전 2초 간격을 두고, HTTP 요청 제한 시간은 30초다.
- 응답을 스트리밍으로 읽으며 2,000,000바이트 초과 시 중단한다.
- 정상 수집 때 원문과 SHA-256·검색어·출처·수집 시각을 보관한다. `complete=false`이며 전국 목록이나 검색 완전성을 보장하지 않는다.
- HTTP·문자 인코딩·정책·크기 제한 실패는 `failure.json`에 별도 기록하고 종료 코드 1을 반환한다. 통신 예외 메시지와 URL을 그대로 출력하지 않는다.
- 실패 시 이전 `search.html`, `receipt.json`은 보존한다. 실패를 빈 매장 목록이나 폐점 증거로 해석하지 않고 성공 수집 시각도 만들지 않는다.
- DB를 읽거나 수정하지 않는다.

## 검증 및 다음 단계

수집기·robots 판단 및 기존 지도 검증 관련 테스트 **127개 통과**. 공개 검색 파라미터, 관리자 페이지 차단 유지, 규칙 우선순위, robots/검색 HTTP 실패, 응답 용량 초과 시 중단, 실패 시 기존 자료 보존을 확인했다. 결과는 `reports/store-brand-review-20261018/paris-tests.xml`에 보관했다. 네트워크 테스트는 모의 HTTP 응답을 사용하며 실제 매장 조회 성공을 뜻하지 않는다.

다음 실제 수집 명령:

```powershell
.venv\Scripts\python.exe -X utf8 -m app.scripts.probe_paris_stores `
  --query 경희 --output reports/store-brand-review-20261018/paris-khu
```

후속 실행에서 실제 응답 구조를 확인하고 파서를 구현했다. 외부 블로그 주소로 DB를 교정하지 않는다. 기존 파리바게뜨 경희대점 후보와 동일한 지점인지 확인한 뒤 별도 주소 교정 또는 신규 등록을 결정한다. 장소 검증과 카드 할인 적용 검증은 계속 분리한다.

이번 수집기 작업으로 매장·혜택을 추가하거나 운영 배포하지 않았다. 마지막 확인된 매장 집계는 50곳, 매장 결제 혜택은 105건이며 기존 미확인 후보 32곳은 별도 검증이 필요하다.

## 후속 실측: 실제 검색 결과와 지역 구분

공식 공개 검색을 두 번 실행해 원문과 검색어·출처·관찰 시각·SHA-256을 보관했다.

| 검색어 | 실제 응답 | 처리 |
| --- | --- | --- |
| 경희 | `data-total-count=1`, 공식 ID **1050**, **수원경희대**, 경기 수원시 영통구 덕영대로 1705 (영통동) | `outside_review_region`으로 제외 |
| 회기 | BOM과 탭만 포함한 응답. HTTP 요청은 성공했지만 목록 구조와 개수 표시가 없음 | `needs_review`, 매장 수는 미확정 |

수원경희대 좌표는 `37.2479663, 127.0778549`이며 서울의 기존 40번 `경희대점 / 회기로 201`과 연결하지 않았다. 회기 응답도 공식적인 0건 목록이 아니므로 폐점·미존재로 해석하거나 기존 매장을 삭제하지 않았다.

`app/services/paris_store_identity.py`는 같은 `.store-list-item` 안의 공식 ID·이름·전체 주소·좌표를 함께 읽는다. 표시된 총개수와 항목 수가 다르거나 ID가 중복되거나 필수 필드·좌표가 잘못되면 거부한다. 명시적인 `<ul data-total-count="0">`만 0건으로 해석한다.

후보 생성 시 보관 응답의 해시, 7일 이내 관찰 시각, 공식 호스트·조회 action·검색어를 검증하고, 공식 ID와 매장명이 모두 정확히 일치해야 한다. 서울 동대문구 밖의 매장은 이번 수집 범위에서 제외한다. 수집기는 `parse_status=parsed` 또는 `needs_review`와 해석 가능한 항목 수를 함께 남기도록 보완했다. 기존 원문 수집 기록은 다시 쓰지 않고 별도 `paris-review.json`으로 해석 결과를 기록했다.

`app/scripts/discover_paris_store.py`는 조건을 통과한 공식 후보를 기존 카카오 SDK 대조 절차로 연결한다. 파리바게뜨는 `bakery` 분류로 등록하며 제공처와 브랜드가 다르면 거부한다. 기본은 조회만 수행하고 `--apply`에서만 백업 후 반영한다. 기존 같은 지점과 주소가 충돌하면 별도 검토를 요구하며 자동 교정하지 않는다. 이번 실제 자료는 지역 조건을 통과하지 못해 등록 CLI를 실행하지 않았다.

수집기·파서·기존 지도 검증 관련 테스트 **152개 통과**. 실제 원문 해석 결과도 별도 스크립트로 확인했다. 분류·카카오 대조 연결의 테스트에는 모의 장소를 사용했으며, 파리바게뜨 실제 장소 대조 성공을 뜻하지 않는다.

자료: `reports/store-brand-review-20261018/paris-khu/`, `paris-hoegi/`, `paris-review.json`, `paris-identity-tests.xml`, `inspect_paris.py`.

현재 집계는 실제 로컬 DB에서 **매장 50곳 / 결제 혜택 105건**으로 확인했다. 서울 후보의 공식 지점 식별은 남아 있으며, 이번 작업에서 DB 수정·운영 배포는 하지 않았다.

## 2026-10-19: 동대문구 지역 조회 및 기존 후보 대조

지점명 검색에서 확인되지 않는 후보를 점검하기 위해 공식 지역 조회를 구현했다. `pb_store_get_area` 응답의 계층에서 `서울 → 동대문구`를 정확히 선택하며, 지역 코드가 없거나 중복되면 요청을 중단한다. 실제 선택 코드는 서울 **117**, 동대문구 **167**이었다. 코드는 하드코딩하지 않는다.

실제 지역 목록과 매장 검색 응답을 각각 보관한다. 영수증은 지역 목록의 해시·선택 지역명과 코드·매장 응답 해시를 묶는다. 재검토 시 보관한 지역 목록이 없거나 코드가 바뀌면 거부한다. 지역 검색은 빈 지점명과 `type=#around`, 해당 `area1`, `area2`만 사용하며 지점명 검색과 동시에 지정할 수 없다.

```powershell
.venv\Scripts\python.exe -X utf8 -m app.scripts.probe_paris_stores `
  --region 서울 동대문구 --output reports/store-brand-review-20261019/paris-dongdaemun

.venv\Scripts\python.exe -X utf8 -m app.scripts.review_paris_region `
  --capture-dir reports/store-brand-review-20261019/paris-dongdaemun `
  --output reports/store-brand-review-20261019/paris-coverage.json
```

실제 응답의 표시 개수와 파싱한 항목 수가 **25개**로 일치했다. 외대후문·휘경·청량리 등의 공식 ID·이름·주소·좌표를 확보했지만, 목록에 `경희대` 또는 `회기`라는 지점명은 없었다. 기존 DB 40번 `경희대점 / 회기로 201`은 정확한 공식 이름 일치가 없어 **pending**으로 유지했다. 지점명 부재만으로 폐점·미존재를 판단하지 않는다.

`review_paris_region.py`는 현재 활성 파리바게뜨 후보를 일괄 대조하는 읽기 전용 CLI다. 이름이 정확히 하나 일치하더라도 주소가 다르면 `needs_address_review`, 이름·건물 주소가 일치하면 `needs_place_check`로 표시할 뿐 DB를 바꾸지 않는다. 동대문구 밖의 신규 매장을 등록하거나 다른 지점을 기존 경희대점에 병합하지 않았다.

이번 지역 수집·대조에서 DB 반영은 없고, 전국/지역 전체 매장이 빠짐없이 수집됐다는 보장은 하지 않는다(`complete=false`). 스케줄러에 연결하거나 운영 배포하지 않았다. 서울 경희대점 후보의 공식 지점 확인은 미완료다.

지역 코드 증거 누락·변조, 계층 내 지역명 중복, 잘못된 코드, 유사 지점명 연결 방지와 주소 불일치 검토를 포함한 관련 테스트 **160개 통과**. 자료는 `reports/store-brand-review-20261019/`의 `paris-dongdaemun/`, `paris-coverage.json`, `tests.xml`에 보관했다.
