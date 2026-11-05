# 카드사 상품·혜택 수집 범위

조사 기준: 2026-10-15. 아래 목록은 수집·파서 검증의 초기 후보이며 발급 추천이나 현재 혜택 적용 보장이 아니다. 공식 상품 안내 존재를 확인한 목록이며 개별 상품의 현재 발급·혜택 상태는 상세 원문 수집 시 다시 판정한다. 아직 이 목록 전체를 DB에 수집한 상태는 아니다.

## 수집 원칙

전체 처리 흐름·DB 스키마·작업 주기·현재 구현과의 차이는 [카드사 혜택 수집 파이프라인 설계](CARD_ISSUER_PIPELINE_DESIGN.md)를 따른다.

카드사별 공개 개인 신용·체크카드 상품 목록 전체를 발견 대상으로 한다. 아래 후보만 하드코딩해 수집 범위를 끝내지 않는다. 초기 후보는 목록 발견→상세 원문→조건 추출→검증→브랜드/매장 연결의 성공 여부를 확인하는 데 먼저 사용한다.

개인 오프라인 매장 혜택을 우선 구조화한다. 온라인 전용·신규 발급 보상·해외 전용·기업/법인카드 등은 별도 범위로 분리하며 오프라인 할인으로 전환하지 않는다. 적립·환급·청구할인·현장할인을 서로 구분한다.

## 1차: 7개 카드사

| 카드사 | 초기 검증 후보 | 공식 근거 |
|---|---|---|
| KB국민 | 노리2 체크카드(KB Pay), WE:SH Daily, My WE:SH | [상품 목록](https://card.kbcard.com/CRD/DVIEW/HCAM0101?pageNo=1&searchwrd=WE%3ASH), [My WE:SH 공식 안내](https://card.kbcard.com/CRD/DVIEW/HCAMCXPRICAC0076?cooperationcode=09923&mainCC=a) |
| 신한 | 처음, MR.Life, Deep Dream(신용·신규 발급 중단 안내 있음) | [처음·MR.Life 안내](https://www.shinhancard.com/pconts/html/landing/1236610_2424.html), [Deep Dream](https://www.shinhancard.com/pconts/html/card/apply/credit/1188220_2207.html) |
| 삼성 | iD ON, iD ALL, iD SELECT ALL | [iD 상품 안내](https://www.samsungcard.com/personal/card/showroom/UHPPCA0212M0.jsp), [추천 상품](https://www.samsungcard.com/home/card/cardinfo/PGHPPDCCardCardinfoRecommendPC001?active=fast) |
| 현대 | Z family Edition2, Z work Edition2, ZERO Edition3(할인형) | [공식 상품·디자인 안내](https://img.hyundaicard.com/HCSP/hyundaicard_MetalPlate_info.html) |
| 하나 | 원더카드2.0 DAILY, 원더카드2.0 FREE+, 원더카드2.0 HAPPY+ | [원더카드2.0 공식 안내](https://m.hanacard.co.kr/MKCDCM1000M.web?CD_PD_SEQ=16950) |
| 우리 | D4카드의정석Ⅱ, 카드의정석 EVERY DISCOUNT, 카드의정석 SHOPPING+ | [공식 대상 상품 안내](https://m.wooricard.com/dcmw/yh1/bnf/bnf02/prgevnt/movePrgEvntDtl.do?evntSrno=30005932), [카드의정석 안내](https://m.wooricard.com/dcmw/yh1/crd/crd01/M1CRD101S08.do?tab=1) |
| 롯데 | LOCA LIKIT, LOCA LIKIT Eat, LOCA LIKIT Shop | [공식 상품 목록](https://www.lottecard.co.kr/app/LPCDARA_V100.lc) |

## 2차: 3개 추가 발급사

| 카드사·발급사 | 초기 검증 후보 | 공식 근거 |
|---|---|---|
| NH농협 | 올바른FLEX, zgm 할인카드, NH올원더풀 | [공식 카드 안내](https://card.nonghyup.com/servlet/IpCc0007M.act) |
| BC 바로 | On&Off, 페이북카드, K-패스 카드 | [공식 상품 목록](https://www.bccard.com/app/card/contents/CardSub.do), [On&Off 상세](https://www.bccard.com/app/card/app/CreditCardMain.do?gdsno=1032030000) |
| IBK기업은행 | I-ALL, 일상의 기쁨(신용), DailyWith(데일리위드) | [I-ALL](https://cardapplication.ibk.co.kr/card/index.jsp?card_prdc_id=102475), [일상의 기쁨](https://cardapplication.ibk.co.kr/card/index.jsp?card_prdc_id=927222), [DailyWith](https://cardapplication.ibk.co.kr/card/index.jsp?card_prdc_id=757777) |

BC는 자체 발급 상품(BC 바로)과 회원 은행 상품이 함께 노출된다. BC 목록과 IBK·NH 등에서 같은 상품을 발견해도 중복 등록하지 않도록 발급사·원천 상품 코드와 교차 출처를 저장한다. K-패스 같은 정책성·교통 혜택이 발견돼도 주변 매장 할인으로 변환하지 않는다.

## 기존 보유자용 추가 수집

- 기존 구현의 펭수 노리 체크카드는 유지한다. 이 상품의 4개 혜택만 현재 자동 검증까지 구현돼 있다.
- 신한 Deep Dream(신용)은 공식 상세에 신규 발급 중단 안내가 있으므로 보유자용 자료로 구분한다.
- BC 바로 GOAT는 공식 공지에 발급 종료가 명시돼 있다. [공식 발급 종료 공지](https://www.bccard.com/app/card/BcnewsViewActn.do?linkTp=&listgo=1&newsno=2027&newsurl=empty_url). 기존 보유자 서비스와 발급 종료를 구분해 수집한다.
- BC 바로 클리어 플러스는 공식 공지 목록에 발급 종료 안내가 있으므로 상세 공지·설명서를 확보한 뒤 상태를 판정한다. [공식 공지 목록](https://www.bccard.com/app/card/https%3A/www.bccard.com/app/card/BcNewsListInqActn.do).

발급 중단을 혜택 종료로 간주하거나 원본 상품을 삭제하지 않는다. 기존/신규 버전, 신용/체크, 선택형 혜택 조합은 별개 상품·조건으로 구분한다. 하나 원더처럼 사용자별 선택 혜택이 달라질 수 있는 상품은 카드 보유 여부만으로 모든 혜택이 적용된다고 판정하지 않는다.

## 추가 우선 출처: 카카오뱅크·토스뱅크

2026-10-15 공식 상세 원문을 확인했다. 기존 30개 후보에 아래 8개 상품 후보를 추가한다. 두 은행은 상품 발견 출처로도 관리하며 제휴 신용카드의 실제 발급사를 별도 저장한다. 아래는 수집 후보 조사 결과이며 자동 수집·검증 구현이나 DB 반영 완료를 의미하지 않는다.

| 출처 | 상품 후보 | 수집·지도 적용 시 구분 | 공식 근거 |
|---|---|---|---|
| 카카오뱅크 | 프렌즈 체크카드 | 기본 캐시백과 실적 조건부 추가 혜택 분리. 디자인은 동일 혜택이면 별도 상품으로 늘리지 않음. 추가 혜택 링크는 앱으로 연결되어 공개 상세·설명서 확보 필요 | [상품 안내](https://www.kakaobank.com/products/checkcard) |
| 카카오뱅크 | 더(The) 체크카드 with 모두의카드 | 선택 혜택팩과 적용 시점, 일·월 한도 저장. 현재 안내의 혜택 기간은 2026-09-21~2027-06-30. 교통 환급은 매장 혜택과 분리 | [상품 안내](https://www.kakaobank.com/products/thecheckcard) |
| 카카오뱅크 | 모임 체크카드 | 모임통장 유지 조건과 기간형 랜덤 캐시백. 확정 할인액·최대값을 보장액으로 표시하지 않음. 현재 프로모션 기간은 2026-01-01~2026-12-31 | [상품 안내](https://www.kakaobank.com/products/moimcheckcard) |
| 카카오뱅크·신한 | 착붙 신한카드 | 신한 발급 제휴 신용카드. 신한 수집 결과와 원천 상품 코드로 통합 | [공식 안내](https://kakaobank.com/products/chakboot) |
| 카카오뱅크·신한 | 줍줍 신한카드 | 기본 캐시백·즉시결제 조건부 추가 캐시백·신규 발급 행사 분리. 신한 상품과 중복 제거 | [공식 안내](https://kakaobank.com/products/plcc) |
| 토스뱅크 | 토스뱅크 체크카드 | 스위치 캐시백 시즌·사용자 선택 모드·브랜드·금액 구간·영역별 횟수 한도 저장 | [상품 안내](https://www.tossbank.com/product-service/card/check-card), [현재 혜택](https://www.tossbank.com/card/benefits) |
| 토스뱅크 | 토스뱅크 K-패스 체크카드 | 공식 상품을 별도 발견하되 공통 카드 혜택과 교통 정책 부가 혜택을 구분. 같은 매장 혜택의 중복 계산 방지 | [공식 안내](https://www.tossbank.com/card/k-pass) |
| 토스뱅크·하나 | 토스뱅크 하나카드 Day | 하나 발급 제휴 신용카드. 공식 소개 확인, 하나의 상세·설명서·상품 코드를 추가 확보한 뒤 혜택 승인 | [공식 소개](https://www.tossbank.com/articles/day-bank) |

### 선택형·기간형 혜택 검증

- 토스뱅크 공식 혜택 본문은 스위치 캐시백 시즌6 기간을 2026-04-01~2027-01-20으로 안내한다. 검색 결과의 과거 종료일을 그대로 저장하지 않고 최신 상세 원문과 변경 이력을 확인한다.
- 토스뱅크는 오프라인·온라인·어디서나·기부 중 사용자 선택 모드 확인이 필요하다. 카카오뱅크 The 체크카드도 선택 혜택팩 확인이 필요하다. 선택을 모르면 조건부 혜택으로 안내하고 확정 적용하지 않는다.
- 토스뱅크 오프라인 모드는 결제액 구간별 정액 캐시백이다. 일률적인 할인율로 바꾸지 않는다. 영역별 당일 첫 결제·일/월 횟수·잔여 한도·간편결제 제외 가능성까지 검증한다. 선택 변경의 실제 효력 시점도 원문대로 저장한다.
- 모임 카드의 랜덤 보상은 최소·최대와 무작위 지급 성격을 저장하며 예상 절약액 순위에 최대 보상을 확정값으로 넣지 않는다.
- 줍줍 카드의 즉시결제 추가 혜택은 사후 행동 조건이다. 해당 조건 충족을 확인할 수 없으면 기본 혜택과 나눠 표시한다.

### 분류와 수집 우선순위

카카오뱅크 카드와 카카오페이, 토스뱅크 카드와 토스페이는 각각 별개 수집 대상이다. 카드 상품 혜택은 카드 할인/캐시백 범주, 페이 결제 행사 자체는 페이 할인 범주에 저장한다. 카드와 페이 혜택의 중복 적용은 공식 조건이 확인된 경우에만 허용한다.

지도 연결 검증은 프렌즈·The·토스뱅크 체크카드부터 진행하고, 제휴 신용카드와 모임 카드를 확장한다. 사업자 카드는 개인 소비 지도 초기 범위에서 제외한다. mini 등 청소년 상품은 별도 후속 조사 대상으로 남긴다. 카카오뱅크 신용카드 플랫폼에서 추가 상품을 발견하면 실제 발급사의 상품과 대조한다. [공식 신용카드 플랫폼](https://www.kakaobank.com/products/cardplatform).

## 후속 범위

케이뱅크와 지방은행의 개인 체크·신용 상품은 별도 출처 조사 후 확대한다.

## 발견 시 저장할 필드

카카오뱅크·토스뱅크 8개 후보의 기간 조사와 권장 확인 주기는 [카드 혜택 갱신 계획](CARD_BENEFIT_REFRESH_PLAN.md)에 정리했다. 실제 발급사의 변경 주기와 서비스 수집 주기를 구분한다.

발급사, 원천 상품 코드, 정식 상품명, 신용/체크 구분, 상품 버전, 공식 상세 URL·설명서 URL, 발급 상태·근거, 마지막 목록 확인 시각, 상세 수집/검증 상태를 기록한다. 혜택별 브랜드·업종·지점·기간·제외·한도·결제 경로는 별도 조건으로 저장한다.

초기 목표는 기존 10개 카드사·발급사의 30개 후보에 카카오뱅크·토스뱅크 출처의 8개 후보를 더한 38개 후보와 기존 보유자용 상품의 수집 검증이다. 출처 수와 발급사 수를 혼동하지 않으며, 실제 고유 상품 수는 제휴 상품 코드 대조 후 확정한다. 최종 수집 범위는 각 공식 목록에서 실제로 발견한 전체 개인 상품이며 정확한 총수는 수집 후 측정한다.
