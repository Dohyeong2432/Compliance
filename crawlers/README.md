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
- **본문 / 제개정사항 / 신구조문대비표**: 아직 미구현입니다
  (`fetch_law_detail`, `crawl_law_items`가 `NotImplementedError`를 던짐).
  law.go.kr 상세 페이지의 실제 HTML 구조를 확인해야 정확한 선택자를 쓸 수
  있어서, 추측으로 셀렉터를 하드코딩하지 않았습니다.

## 이어서 구현하려면 (택 1)

1. **스크래핑 계속 (Selenium/BeautifulSoup)**: 다음 페이지들의 HTML(뷰
   소스)을 공유해주세요.
   - 법령 본문 조회 페이지 (조문이 나열된 화면)
   - 행정규칙 본문 조회 페이지
   - 신구조문대비표 페이지
   - (선택) 실제로 `crawl_listing()`을 한 번 돌려서 나온 `*__href`/`*__onclick`
     값 샘플 — 상세 페이지로 가는 링크를 코드가 어떻게 해석해야 하는지 확인용
2. **law.go.kr Open API 사용 (권장)**: [open.law.go.kr](https://open.law.go.kr)에서
   OC 인증키를 발급받으면, `lawService.do` 등으로 조문 본문을 XML/JSON으로 바로
   받을 수 있어 Selenium 없이 `requests`만으로 훨씬 안정적으로 구현할 수
   있습니다(신구조문대비 정보 제공 여부는 발급받은 API 문서로 재확인이
   필요합니다). OC 키가 있다면 알려주세요 — 목록 검색도 이 API로 통일해서
   Selenium 의존성 자체를 없애는 것도 가능합니다.

## `pipeline.connectors.law.LawConnector`에 연결하는 법

본문 파싱이 완성되면, `crawl_law_items()`가 다음 스키마의 `list[dict]`를
반환하도록 맞춥니다(자세한 스키마는
[`pipeline/connectors/crawler_base.py`](../pipeline/connectors/crawler_base.py) 참고):

```python
{
    "id": "capital-markets-act-46",         # 법령/행정규칙 고유 식별자
    "title": "자본시장과 금융투자업에 관한 법률 제46조",
    "body": "...",                            # 조문 본문 (+ 필요시 제개정이유)
    "effective_date": "2023-07-01",
    "supersedes": "law:capital-markets-act-46-v1",  # 신구조문대비표에서 얻은 구법 id
    "source_url": "https://www.law.go.kr/...",
}
```

그 다음 `.env`에 `LAW_CRAWLER=crawlers.law_go_kr:crawl_law_items`를 지정하면
서버가 시작 시 + 주기적으로 자동 호출합니다(루트 README.md "자동
재색인/동기화" 참고). 행정규칙도 같은 `EntityType.LAW`로 색인됩니다 — 사내
`REGULATION`과는 성격이 달라(정부기관 고시/훈령/예규 vs 사내 규정) 구분해
두었습니다. 다르게 분류하고 싶으면 알려주세요.
