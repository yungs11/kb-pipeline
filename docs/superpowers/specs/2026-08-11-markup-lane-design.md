# 구조화 텍스트 레인 설계 — html/csv/json/xml 을 형변환 API 밖으로

작성 2026-08-11 · 브랜치 `feat/markup-lane`

## 1. 배경과 문제

`html` 은 한컴 형변환 API(`tools/fileconvert.py`)로 **PDF 를 만든 뒤** ODL 로 파싱한다.
어차피 최종 산출물이 markdown 청크인데 PDF 를 경유하므로 원격 API 왕복·실패 지점이
공짜로 늘어난다. 폐쇄망에는 그 API 가 아예 없을 수 있다.

**착수 전 실측으로 확인한 현재 동작** (전제가 일부 틀렸다):

| 확장자 | 실제 경로 | 근거 |
|---|---|---|
| `html`/`htm` | 형변환 API → PDF → ODL | `fileconvert.CONVERTIBLE_EXTS` (`tools/fileconvert.py:29`) |
| `csv`, `json` | 변환 안 탐 — text 레인에서 **원문 그대로** blockify | `TEXT_EXTS` → `router._text_parse` (`router.py:34`) |
| `xml` | 어느 목록에도 없음 → `pdf` 도메인 → `%PDF` 가드에서 **실패** | `router.domain_of` (`router.py:70`), `app.py:266` |

즉 형변환 API 를 타는 것은 html 뿐이고, csv 는 구조를 잃고, xml 은 `not a PDF (and not
convertible)` 로 죽는다.

## 2. markitdown 검토와 기각 (실측 근거)

당초 [microsoft/markitdown](https://github.com/microsoft/markitdown) 적용을 검토했다.
markitdown 0.1.7 / Python 3.12 로 실제 변환을 돌려 다음을 확인했다.

**2.1 html 표가 깨진다.** `rowspan=2`/`colspan=2` 가 있는 표를 넣으면:

```
| 구분 | 금액 | |        ← 헤더 3열
| --- | --- | --- |
| 2025 | 2026 |         ← 데이터행이 2셀 (열 정렬 붕괴)
| 본사 | 10 | 20 |
```

병합셀 손실을 넘어 **열 정렬 자체가 깨진다**. 이 리포의 불변식은 "표는 `<table>` HTML
보존, pipe 평탄화 금지"다.

**2.2 이미 같은 이유로 제거된 이력이 있다.** markitdown 은 Phase 2d(`a8f9818`)에서
코드·requirements 에서 완전 제거됐고 재유입 가드 `parse_service/tests/test_no_markitdown.py`
가 걸려 있다. 사유는 동일하다 — `docs/kb-pipeline-process-definition.md:243`: "markitdown 이
pptx/DOCX 의 병합(colspan/rowspan)을 파싱 시점에 소실(측정: 05 PPTX colspan 68/rowspan 43
vs markitdown 0/0)". 채택하려면 **기존 ADR 을 뒤집고 그 가드를 삭제**해야 한다.

**2.3 json/xml 은 변환하지 않는다(passthrough).** 소스와 계측 두 가지로 확인했다.

```
fx/t.json  -> HANDLED BY PlainTextConverter   원문과 완전 동일: True
fx/t.xml   -> HANDLED BY PlainTextConverter   원문과 완전 동일: True
fx/t.csv   -> HANDLED BY CsvConverter         원문과 완전 동일: False
fx/t.html  -> HANDLED BY HtmlConverter        원문과 완전 동일: False
```

- 등록 컨버터 목록(`_markitdown.py:182-205`)에 json/xml 전용이 없다(`RssConverter` 는
  RSS/Atom 전용).
- `PlainTextConverter.ACCEPTED_FILE_EXTENSIONS` 에 `.json`/`.jsonl` 이 **명시적으로** 있다 —
  설계상 평문 취급이다. xml 은 mimetype `text/xml` 이 `"text/"` prefix 에 걸려 같은 경로.
- `PlainTextConverter.convert()` 본문은 디코드 후 `DocumentConverterResult(markdown=text)`
  가 전부다(변환 로직 0줄).

**2.4 비용.** base 설치만으로 site-packages **+140MB**(onnxruntime 69M, sympy 29M,
numpy 27M, magika 3M). magika 는 파일타입 sniffing 용인데 우리는 확장자로 이미 라우팅한다.

**2.5 우리가 실제로 쓰게 될 부분은 얇다.** html 컨버터(110줄)의 실체는 script/style 제거
3줄 + `_CustomMarkdownify`(= markdownify 서브클래스) 호출 + RecursionError 폴백이다. csv
컨버터(77줄)는 `csv.reader` + pipe 표 조립 30줄이고 **셀 안의 `|` 를 이스케이프하지 않는다**.

**결정: markitdown 을 도입하지 않는다.** 재유입 가드 `test_no_markitdown.py` 를 유지한다.
html→markdown 엔진으로 markitdown 이 내부에서 쓰는 `markdownify` 만 직접 쓴다.

## 3. 목표 / 비목표

**목표**
- html/htm 을 형변환 API 없이 파싱하고 표는 `<table>` HTML 로 보존한다.
- csv 를 구조 있는 청크로 만든다.
- xml 이 실패하지 않게 한다.
- 폐쇄망 부담을 최소화한다(신규 pip 의존성 1개).

**비목표(→ `deferred.md`)**
- json/xml 의 구조 변환(키 계층 → 헤딩/표). 지금은 평문 통과.
- tsv·세미콜론 구분자, epub/zip 등 신규 입력 포맷.
- 초대형 csv 의 청크 수 폭증 — 엑셀 레인이 xlsx 에서 이미 갖는 동일 문제라 여기서 따로
  풀지 않는다.
- 표 없는 html 의 markdown 스타일 튜닝.

## 4. 설계

### 4.1 라우팅 변경

```
CONVERTIBLE_EXTS -= {html, htm}     # 형변환 API 는 hwp hwpx doc docx ppt pptx 전용으로 축소
TEXT_EXTS        -= {csv}, += {xml} # csv 는 엑셀 레인으로, xml 은 평문 통과
EXCEL_EXTS       += {csv}           # csv 를 엑셀 레인이 받는다
domain_of()      += "html" 분기      # html/htm → parsers/html
```

도메인 5개: `excel` / `ocr` / `html` / `text` / `pdf`. 이미지·PDF 레인 무영향.

`domain_of()` 분기 순서는 **excel → ocr → html → text → pdf**. `TEXT_EXTS` 에서 `csv` 를
빼는 것과 `EXCEL_EXTS` 에 넣는 것은 **함께** 해야 한다(둘 다 하지 않으면 순서에 따라
조용히 옛 레인으로 샌다).

### 4.2 html → `parse_service/parsers/html/`

1. bs4 로 파싱 → `<script>`/`<style>` 제거 → `<body>` 있으면 그것을 대상으로
2. **최상위 `<table>` 을 원문 HTML 문자열로 보관**하고 그 자리에 sentinel 텍스트로 치환.
   중첩 표는 바깥 표에 통째로 포함되므로 top-level 만 추출한다(`table` 조상이 없는 것).
   sentinel 은 markdownify 이스케이프를 타지 않는 형태로 고른다(대문자 영숫자).
3. 나머지를 `markdownify` → markdown
4. sentinel 을 **앞뒤 빈 줄과 함께** 원본 `<table>` HTML 로 복원 → markdown-it 이
   `html_block` 으로 인식한다(빈 줄이 없으면 문단 안 inline HTML 이 되어 blockify 의
   table 분기를 타지 못한다)
5. `kb_pipeline.blockify.hybrid_to_blocks()` → `{type:"table", table_body:"<table>…"}`.
   colspan/rowspan 그대로 생존.

반환은 `RouteResult(kind="pages", chunk_needed=True, pages=[{page_number:1, blocks:[…]}])`
— 기존 text 레인과 같은 계약이다(단일 페이지).

디코딩은 text 레인의 사다리를 재사용한다: BOM 이 있을 때만 `utf-16`, 그 다음
`utf-8-sig` → `cp949`. `errors="replace"` 금지(U+FFFD 범벅이 '성공한 쓰레기'가 된다).
빈 결과는 `ParserError`.

### 4.3 csv → 엑셀 레인 위임

csv 를 **메모리상 xlsx 로 합성**해 기존 openpyxl 백엔드에 넘긴다. 실측 결과 행 단위
레코드 청크가 나온다:

```
사번: 1001, 성명: 김철수, 부서: 전략기획부, 직위: 부장, 입사일: 2015-03-02, 연봉: 98000000
```

**헤더 행에 서식(볼드 + 채우기)을 반드시 부여한다.** `_detect_header_rows` 의
`strong` 판정이 `eff_style >= _STYLE_GATE_MIN` 을 요구하므로
(`detection/header_detector.py:318-323`, `strong` 판정), 서식 없는 맨 셀로 합성하면 헤더 감지가
**구조적으로 실패**하고 `key = headers.get(c) or get_column_letter(c)`
(`parsers/flat_table.py:175`) 폴백이 걸려 `A: 1001, B: 김철수` 로 퇴화한다(실측 확인).
헤더 행 자체도 데이터행으로 오인돼 청크가 하나 늘어난다(실측: `table_row` 4 → 서식 부여 후 3).

**시트명과 `document_title` 에 원본 csv 의 basename stem 을 쓴다.** 청크 텍스트에 시트명이
그대로 박히므로(`… > Sheet1 > 1001`) 임시파일 stem 이 새면 검색어에 런타임 잡음이 섞인다.
엑셀 레인이 xlsx 에 대해 이미 같은 방어를 하고 있다(`parsers/excel/__init__.py`
`_fetch_rag_chunks` 주석).

**백엔드는 `openpyxl` 로 고정한다.** 기본값 `EXCEL_PARSER_BACKEND=auto` 는 "전결" 키워드
(Tier1)나 계층 지배도(Tier1.5)가 있을 때만 openpyxl 을 쓰고 **그 외에는 kordoc 으로
떨어진다**(`backends/auto_backend.py:88-150`). csv 유래 평면 표는 둘 다 아니므로 auto 로
두면 kordoc CLI 왕복을 타는데, csv 에는 병합셀·다중시트·수식이 없어 kordoc 의 렌더 충실도
이점이 전혀 없고 node 프로세스 비용만 든다. 실측에서도 auto 로 두면
`'excel_parser_*.md' 를 찾을 수 없습니다` 로 실패했다(호스트에 `KORDOC_BIN` 미설정 시).

디코딩 사다리는 4.2 와 동일. 구분자는 콤마 고정(비목표).

게이트는 정상 통과함을 실측했다 — 정상/단일열/헤더만/빈값많음 4형태 모두 `gate.ok=True`,
findings 0.

**결과 계약**: 엑셀 레인 그대로 `kind="chunks"`, `chunk_needed=False`, `gate_summary` 포함.

**한계(수용됨, 2026-08-11 사용자 확인)**: 첫 컬럼 헤더가 계층 spine 용어
(`사항/항목/구분/내용/업무/분류/제목/품목/품명`)를 **포함**하면 `_detect_hierarchy_cols`
(`detection/header_detector.py:476`)가 그 열을 계층 열로 잡아, 키 이름이 `항목:` 이 아니라
`A:` 로 떨어진다(실측 4종 중 3종). **값은 청크 경로(`titles_context`)로 승격돼 보존**되므로
검색에서 잡히고, 잃는 것은 키 이름과의 결합뿐이다. 이것은 엑셀 레인이 실제 xlsx 에도 적용하는
의도된 휴리스틱이고 끄는 스위치가 없어(`RegionOverride.hierarchy_cols` 는 비어 있지 않을
때만 적용) csv 만 예외 처리하지 않는다. `deferred.md` D45.

### 4.4 json/xml → 평문 통과

`TEXT_EXTS` 에 `xml` 추가. json 은 현상 유지. 코드 변경은 이것뿐이다.

### 4.5 실패 처리

`ParserError` → `app.py` 가 `parse_failed` 로 매핑(기존 계약). **형변환 API 로의 폴백은
두지 않는다** — 폐쇄망에 그 API 가 없을 수 있고, 조용한 회귀 경로가 된다.

## 5. 청킹 소유권 변경 (문서 반영 필요)

csv 의 청킹 소유가 facade `/chunk` → 엑셀 레인으로 이동한다. 엑셀은 이미 인정된 예외
레인(`chunk_needed=False`)이므로 불변식 위반은 아니지만, `_workspace/01-architecture.md`
의 라우팅 표(§ 확장자→도메인)와 "청킹 소유" 서술에 반영해야 한다.

## 6. 폐쇄망

- **신규 pip 의존성은 `markdownify` 하나.** `requirements.txt` 에만 추가한다(parse-svc
  전용 의존성 관례 — `opendataloader-pdf`/`PyMuPDF`/`openpyxl` 이 이미 그렇다).
  `pyproject.toml` 은 건드리지 않는다(`requires-python = ">=3.9"` 충돌 회피).
- `beautifulsoup4`/`lxml`/`openpyxl` 은 이미 있다.
- **env 신설·삭제·기본값 변경 없음** → `.env.example` 계열 6종 수정 불필요.
- `scripts/airgap/verify-bundle.sh` 의 import 검사에 `markdownify` 를 추가한다(누락하면
  폐쇄망에서만 죽는다 — `kb-image-missing-doc-extractors` 전례).

## 7. 검증

- **V1** colspan/rowspan 이 든 html → `table_body` 에 `colspan`/`rowspan` 속성 생존
- **V2** html 이 형변환 API 를 타지 않음 — `needs_convert("a.html") is False`
- **V3** 표 여러 개·중첩 표 html → 표 개수 일치, 중첩 표가 이중 계산되지 않음
- **V4** csv → 청크 텍스트에 `사번: 1001` 형태(열레터 `A:` 가 아님)
- **V5** csv 청크에 임시파일 stem 이 새지 않음(시트명·`titles_context` 가 원본 stem)
- **V5b** csv 가 kordoc 이 아니라 openpyxl 백엔드로 감(`KORDOC_BIN` 없이도 성공)
- **V6** cp949 csv/html 이 mojibake 없이 디코딩
- **V7** `xml` 이 `not a PDF` 로 죽지 않음
- **V8** `test_no_markitdown.py` 통과 유지
- **V9** 폐쇄망 가드: `verify-bundle.sh` 의 import 검사에 `markdownify` 가 포함되고 실제로
  실행됨(만들어만 두고 안 돌리면 없는 것과 같다 — `guard-exists-but-never-ran` 전례)
- **V10** 전체 회귀: `pytest parse_service/tests tests service/tests` (베이스라인 646 passed)
