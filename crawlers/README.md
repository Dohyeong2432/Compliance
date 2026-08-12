# 크롤러 (law.go.kr)

`pipeline/connectors/*`는 크롤링 자체를 하지 않습니다 — `LawConnector`처럼
`fetch_items` 콜백을 주입받아 그 결과를 온톨로지로 변환하는 역할만 합니다.
실제로 사이트에 접속해 데이터를 긁어오는 코드는 이 디렉터리에 둡니다.

## 설치

메인 앱(`requirements.txt`)과 분리되어 있습니다. 브라우저 자동화가
필요한 이 크롤러는 API 서버 배포 컨테이너에는 불필요하므로:

```bash
pip install -r crawlers/requirements.txt
```

Selenium이 Chrome을 직접 실행하므로 로컬 PC(또는 Chrome이 설치된 환경)에서
실행하는 걸 전제로 합니다.

## 현재 상태: `crawlers/law_go_kr.py`

- **목록 검색(법령명·공포일자 / 행정규칙명·발령일자)**: 동작합니다.
  `crawl_listing(browser, "law")` / `crawl_listing(browser, "reg")`.
- **본문 파싱**: `parse_law_body_html()`이 실제 "본문" 버튼(`#bdyBtnKO`) 클릭
  결과 HTML을 파싱합니다 — 법령 메타데이터(법령ID/명/공포번호·일자/시행일,
  `lsId`/`lsNm`/`ancNo`/`ancYd`/`efYd` hidden input 기반), 조문별 본문, 부칙까지
  추출하며 `tests/test_law_go_kr_crawler.py`가 실제 law.go.kr 결과 HTML
  fixture(`tests/fixtures/law_go_kr_body.html`)로 검증합니다.
  - **"신구조문대비표"에 대한 발견**: 별도 페이지(`lsOldAndNew`) 없이도, 시행
    예정 개정이 걸린 법령은 본문 페이지 자체가 조문마다 "현행 텍스트"
    (`<div class="pgroup">`)와 "시행예정 텍스트"(`<div class="pgroup babl">`,
    바뀐 부분 빨간색)를 **함께** 보여줍니다. `parse_law_body_html()`이 이
    두 벌을 각각 뽑아 `law_detail_to_items()`에서 `SUPERSEDES` 관계로 연결하므로
    (현행 버전의 `superseded_date` = 시행예정 버전의 시행일), **"미래 개정
    대비"는 이미 됩니다.** 다만 이건 "지금 vs 곧 시행될 버전" 비교이지,
    "예전 개정 vs 그 이전"처럼 과거 이력 간 비교는 아닙니다 — 그게 필요하면
    `lsOldAndNew`/연혁 페이지 HTML을 추가로 공유해주세요.
  - **제정·개정이유**(`lsRvsDocInfoR`)는 아직 별도 파싱 대상이 아닙니다 —
    본문 페이지의 `[전문개정 YYYY.MM.DD.]` 같은 각주 수준으로만 딸려 옵니다.
- **목록 → 본문 페이지 이동**: 실제 사용 중이라고 확인해주신 참고 코드
  (`click_law_row`)를 그대로 옮겨서 구현했습니다 — href/onclick을 저장했다가
  재생하는 게 아니라, "번호" 컬럼으로 그 행을 목록 페이지에서 다시 찾아 안의
  `<a>`를 살아있는 DOM에서 바로 클릭하는 방식입니다.
  - `click_row_by_number(browser, row_number)` — 현재 페이지에서 그 행을
    찾아 클릭 (mock 기반 단위 테스트로 검증됨, `tests/test_law_go_kr_crawler.py`).
  - `wait_for_detail_page(browser, expected_name)` — `<h2>` 텍스트가
    `expected_name`으로 시작할 때까지 대기. 참고 코드는 정확히 일치(`==`)를
    봤는데, 실제 본문 페이지의 `<h2>`는 "법령명 ( 약칭: ... )"처럼 약칭이
    붙어 나온다는 걸 이번에 받은 실제 HTML로 확인해서, 약칭이 있어도 걸리지
    않도록 접두어 일치로 고쳤습니다.
  - `open_law_detail_by_name(browser, site_category, law_name)` — 이름을
    아는 법령 하나를 목록에서 찾아 여는, 참고 코드와 가장 가까운 진입점.
  - `crawl_law_items()` — 목록의 모든 행을 순서대로 열었다가 같은 페이지로
    돌아와 다음 행을 여는 전체 수집 루프. `click_row_by_number`/
    `wait_for_detail_page` 자체는 검증된 로직이지만, 이 "전부 순회" 반복은
    참고 코드에 없던 확장이라 아직 실제 사이트에서 통짜로 돌려보지는
    못했습니다 — 처음엔 `crawl_law_items(from_date=...)`로 범위를 좁히거나,
    이름 하나로 `fetch_law_item_by_name()`을 먼저 확인해보길 권합니다.

## 더 다듬고 싶다면

1. `crawl_law_items()`나 `fetch_law_item_by_name()`을 실제로 한 번 돌려서
   결과를 알려주시면(성공/실패, 어느 지점에서 막히는지) 이어서 고치겠습니다.
2. 과거 개정 이력 간 비교(연혁/신구법비교 페이지)가 필요하면 그 페이지의
   HTML을 공유해주세요.
3. **law.go.kr Open API 사용(대안)**: [open.law.go.kr](https://open.law.go.kr)에서
   OC 인증키를 발급받으면 `lawService.do` 등으로 조문 본문을 XML/JSON으로 바로
   받을 수 있어 Selenium 없이 `requests`만으로 더 안정적으로 구현할 수
   있습니다. OC 키가 있다면 알려주세요 — 목록 검색까지 API로 통일해 Selenium
   의존성 자체를 없애는 것도 가능합니다.

## `pipeline.connectors.law.LawConnector`에 연결하는 법

`law_detail_to_items()`가 `pipeline/connectors/crawler_base.py`가 기대하는
`list[dict]` 스키마로 이미 변환해줍니다. 조문 하나가 시행 예정 개정을
가지고 있으면 이렇게 두 항목이 나옵니다(실제 `tests/test_law_go_kr_crawler.py`
결과 예시):

```python
{
    "id": "011359-1-0@2026-08-04",          # 현행 버전
    "title": "전기통신금융사기 피해 방지 및 피해금 환급에 관한 특별법 제1조(목적)",
    "body": "...",
    "effective_date": "2026-08-04",
    "superseded_date": "2026-10-01",         # 시행예정 버전이 있으면 자동으로 채워짐
    "source_url": "https://www.law.go.kr/...",
},
{
    "id": "011359-1-0@2026-10-01",          # 시행예정 버전
    "title": "전기통신금융사기 피해 방지 및 피해금 환급에 관한 특별법 제1조(목적)",
    "body": "...",
    "effective_date": "2026-10-01",
    "supersedes": "law:011359-1-0@2026-08-04",  # SUPERSEDES 관계 자동 생성
    "source_url": "https://www.law.go.kr/...",
}
```

그 다음 `.env`에 `LAW_CRAWLER=crawlers.law_go_kr:crawl_law_items`를 지정하면
서버가 시작 시 + 주기적으로 자동 호출합니다(루트 README.md "자동
재색인/동기화" 참고). 행정규칙도 같은 `EntityType.LAW`로 색인됩니다 — 사내
`REGULATION`과는 성격이 달라(정부기관 고시/훈령/예규 vs 사내 규정) 구분해
두었습니다. 다르게 분류하고 싶으면 알려주세요.
