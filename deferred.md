# deferred — 범위 밖으로 미룬 것

각 항목은 **왜 이번 범위가 아닌지**와 **언제 필요해지는지**를 함께 적는다.
번호는 기존 문서(`_workspace/*`)에서 쓰던 D 번호를 잇는다(직전 최대 D38).

- **D39 json/xml 구조 변환** (2026-08-11, markup-lane) — 지금은 평문 통과.
  markitdown 도 하지 않는 일이라(`PlainTextConverter` passthrough, 실측) 기능 손실은
  없다. 정형 API 응답 같은 문서가 실제로 들어오기 시작하면 키 계층 → 헤딩/표 변환을
  검토한다.
- **D40 tsv·세미콜론 구분자 csv** (2026-08-11, markup-lane) — 구분자 콤마 고정.
  `csv.Sniffer` 는 오작동 위험이 있어 실제 요구가 생길 때 붙인다.
- **D41 초대형 csv 의 청크 수 폭증** (2026-08-11, markup-lane) — 행당 1청크라 10만 행이면
  10만 청크다. 엑셀 레인이 xlsx 에 대해 이미 갖는 동일 문제라 여기서 따로 풀지 않았다.
  적재 지연·비용이 실제로 문제가 되면 엑셀 레인 차원에서 함께 다룬다.
- **D42 엑셀 레인 청크 메타의 임시파일 stem 누출** (2026-08-11, markup-lane) —
  `parsers/excel/__init__.py` 의 `prefix="excel_parser_"` 탓에 청크 `id`·`keywords` 에
  `excel_parser_<random>` 이 남는다. 청크 **본문**(`text`)은 `document_title` 로 덮여
  깨끗하므로 이번 목표(검색 품질)에는 영향이 없다. xlsx 레인이 이미 갖고 있던 문제이며
  csv 편입과 무관하다. 키워드 검색 오염이 관측되면 그때 함께 고친다.
- **D43 pytest testpaths 에 `parse_service/tests` 미포함** (2026-08-11, markup-lane) —
  `pyproject.toml` 의 `testpaths` 가 `["tests", "service/tests"]` 라 맨 `pytest` 는
  parse-svc 테스트를 수집하지 않는다. 이번 작업은 항상 경로를 명시해 무해하지만,
  CI·타인 실행 시 조용히 안 도는 부류다. 베이스라인 수치가 바뀌므로 별건으로 처리한다.
- **D44 xml/html 업로드 allowlist(kb-backend 측)** (2026-08-11, markup-lane) —
  이 리포에는 업로드 allowlist 가 없다(grep 확인). 상위 kb-backend 가 확장자를 막고
  있으면 사용자 관점의 "xml 실패 해소"가 미완일 수 있다. 별도 리포라 이번 범위 밖.
  같은 맥락으로, csv 가 `gate_summary` 를 갖게 되면서 호출자가 gate_summary 보유 문서를
  일괄로 엑셀 게이트에 태울 경우 csv 가 처음으로 blocking 게이트를 받게 된다 — 소비처
  결정이라 이 리포 밖이다(실측상 csv 는 `gate.ok=True`).
- **D45 첫 컬럼 헤더가 계층 spine 으로 흡수돼 키 이름이 `A:` 로 떨어진다**
  (2026-08-11, markup-lane) — 첫 컬럼 헤더에 `사항/항목/구분/내용/업무/분류/제목/품목/품명`
  중 하나가 **포함**되면(`제품명` ⊃ `품명`) `_detect_hierarchy_cols`
  (`detection/header_detector.py:476`, `_ITEM_HEADER_TERMS`)가 그 열을 계층 열로 잡고,
  `flat_table._row_fields`(`:175`)의 `headers.get(c) or get_column_letter(c)` 폴백이
  걸려 `항목: 임차료` 가 아니라 `A: 임차료` 가 된다.

  **실측**(4종 중 3종 해당):
  ```
  사번,성명,부서      → 사번: 1001, 성명: 김철수, 부서: 전략기획부   (온전)
  항목,금액,비율      → t > 임차료 │ A: 임차료, 금액: 1000, 비율: 10
  구분,2025년,2026년  → t > 본사   │ A: 본사, 2025년: 10, 2026년: 20
  제품명,단가,수량    → t > 볼펜   │ A: 볼펜, 단가: 1000, 수량: 5
  ```

  **값은 유실되지 않는다** — 해당 셀 값이 청크 경로(`titles_context`)로 승격돼 있어
  검색에서 잡힌다. 잃는 것은 그 열의 **키 이름과의 결합**뿐이다.

  범위 밖으로 둔 근거: (a) 이것은 엑셀 레인이 실제 xlsx 에도 똑같이 적용하는 **의도된**
  계층 휴리스틱이고, (b) csv 만 예외 처리하면 xlsx 와 동작이 갈리며, (c) 끄는 스위치가
  없다 — `RegionOverride.hierarchy_cols` 는 `if ov.hierarchy_cols:` 로 **비어 있지 않을
  때만** 적용돼 "계층 열 없음"을 지정할 수 없다(`detection/region_detector.py:100`).
  끄려면 엑셀 레인 본체 수정이 필요하고 전결/매트릭스 동작에 회귀 위험이 있다.
  검색 품질에서 실제 문제가 관측되면 그때 엑셀 레인 차원에서 다룬다. (사용자 확인 완료.)
- **D46 단일열 csv / 헤더 없는 csv** (2026-08-11, markup-lane) — `strong` 판정이
  `m["filled"] >= 2` 를 요구해 1열짜리 표는 헤더를 못 잡는다(실측). 또 `csv_to_xlsx` 는
  **첫 행을 헤더로 간주**하므로(csv 포맷 관례) 헤더 없는 파일이면 첫 데이터 행이
  컬럼명이 된다. 둘 다 헤더 감지 휴리스틱 문제라 D45 와 같은 이유로 범위 밖.
- **D47 blockify 에 코드펜스 분기 없음** (2026-08-11, markup-lane) — markdownify 가
  `<pre>` 를 ``` 펜스로 내보내는데 `hybrid_to_blocks` 에 fence 분기가 없어 본문이
  사라진다(실측). html 레인만의 문제가 아니라 `.md` text 레인 공통이라 blockify 본체
  수정이 필요하다 — 이번 html 범위를 넘는다.
  **관련 부수효과**: text 레인의 빈 블록 가드 때문에, 코드펜스만 있는 `.md`·`---` 만 있는
  파일은 이제 조용한 빈 적재 대신 `parse_failed` 로 크게 실패한다(의도된 선택).
- **D48 `verify-bundle.sh` 의 이미지 선택 구멍** (2026-08-11, markup-lane) —
  `grep -m1` 이 스토어에 남은 **옛** `kbp-parse-svc` 를 집을 수 있다(같은 사고가 이미
  기록돼 있다: "옛 이미지가 가드를 속인다"). 이번 실행에서는 로그의 ref 를 눈으로 확인해
  방금 빌드한 태그임을 확인했다(`kbp-parse-svc:markup-lane`). 번들 출처 확인 자동화는 별건.
- **D49 html 의 상대·원격 `<img src="logo.png">`** (2026-08-11, markup-lane) —
  `_strip_data_uri_images` 는 data-URI 만 다루므로 상대경로 이미지는 해소 불가능한
  `img_path` 를 가진 모달 블록으로 남는다. `KBP_MODAL_ENRICH` 기본값이 0 이라 지금은
  payload 통과일 뿐 무해하지만, html 레인에 모달 강화를 켜면 실제 실패가 된다.
  그때 alt 대체 또는 이미지 fetch 를 정한다.
- **D50 `decode_text` 가 `<meta charset>` 을 스니핑하지 않는다** (2026-08-11, markup-lane) —
  BOM/utf-8/cp949 밖 인코딩(shift_jis, iso-8859-1)의 html 은 크게 실패하거나 cp949
  모지바케가 된다. 기존 text 레인과 동일 동작이라 **회귀는 아니다**. 비한국어 html 이
  입력으로 들어오기 시작하면 필요해진다.
