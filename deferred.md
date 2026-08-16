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
- **D51 doc_guard 의 `.xls`** (2026-08-13, markup-lane) — **실측으로 해소(기우였다).**
  `SUPPORTED_EXTENSIONS = {".docx",".pdf",".xlsx"}` 는 multipart `/v1/check` 경로 전용인데
  그 경로는 **호출부가 0건**이다(kb `pipeline.py:338` 주석도 같은 사실을 적고 있다).
  실제 엑셀 게이트는 kb → facade `/gate/check-excel` → doc_guard `/v1/check-excel` 로
  **parse-svc 가 만든 `gate_summary` 를 받아 리포트만 만든다** — 파일 확장자와 무관하다.
  실측(doc_guard :8001): `broken_formula.xls` → `result=fail`,
  `ref_error / 참조오류 시트 H3, J9`, `legacy_sample.xls` → `unclear_header`. 무검문 적재 아님.

- **D52 kb-backend `document_signals` 의 `.xls`** (2026-08-13, markup-lane) —
  `_EXTRACTORS` 에 `xls` 키가 없어 텍스트 폴백으로 degrade 한다(페이지수를 지어낸다).
  D51 과 같은 이유로 이번 배포 후에 관측 가능해진다.
- **D53 암호화 OOXML 이 CFB 매직에 걸린다** (2026-08-13, markup-lane) —
  `\xD0\xCF\x11\xE0` 는 BIFF 전용이 아니다(구 `.doc`/`.ppt`, 암호 걸린 `.xlsx` 도 CFB).
  암호 xlsx 는 **오늘은 openpyxl 이 즉시 실패**하는데, 변경 후엔 soffice 변환
  타임아웃(60s)까지 워커를 점유한 뒤 실패한다. 최악 점유 = 워커수 × 60s.
  `EncryptedPackage` 스트림 탐지로 한 겹 좁힐 수 있다 — 실제 유입이 관측되면 착수.
- **D54 `.xls` 이름 + 비CFB·비zip 바이트** (2026-08-13, markup-lane) —
  HTML 표·탭구분 텍스트를 `.xls` 로 저장한 실무 파일. 이번 범위는 **BIFF 개통**이라
  매직바이트가 CFB 가 아니면 변환을 타지 않고 오늘과 동일하게 실패한다(테스트 케이스 ⑤로
  고정). 지원하려면 별도 스니핑 레인이 필요하다.
- **D55 이름만 `.xls` 인 zip(실체 xlsx)** (2026-08-13, markup-lane) —
  지금은 확장자를 보존해 kordoc 경로로 간다(오늘과 동일). xlsx 레인으로 보내는 편이 "더
  올바른" 처리일 수 있으나 라우팅(Tier1/1.5)이 바뀌는 동작 변경이라 이번 목표 밖으로 뒀다.
- **D56 전결 `.xls`** (2026-08-13, markup-lane) — **실측으로 닫힘.**
  실제 위임전결 문서(`위임전결규정_병합셀_6p-35p.xlsx`, 72KB)를 `MS Excel 97` 필터로
  `.xls`(120KB) 변환해 원본과 비교:

  | | routed_backend | delegation_rule |
  |---|---|---|
  | 원본 `.xlsx` | `openpyxl` | **207** |
  | `.xls` → 변환본 | `openpyxl` | **207** |

  청크 본문도 동일(`… 물품 구입 신청 항목군의 전결 기준: - 50만원 초과 …: 총장 결재:○`).
  변환 전이라면 확장자 게이트에 막혀 kordoc 으로 떨어져 **207 → 0** 이 됐을 자리다.
  픽스처로 커밋하지는 않았다(타 프로젝트 코퍼스 120KB) — 재현 절차만 여기 남긴다.
- **D57 [별도 레포] kb-pipeline 재적재 중복판정 버그 — 수정 완료(2026-08-16 밤)**.
  plan: `~/.claude/plans/d57-reupload-dedup-fix.md`(v2 READY, ultracode 2라운드).
  **확정됐다** — 라이브 postgres 직접 조회로 실증: 같은 kb_id+file_name 조합이 10행까지
  쌓이고 그중 ready 상태만 7개, 전부 서로 다른 `docs_id`(재적재마다 edgequake 에 별개
  사본이 쌓여 검색 시 최신/구판이 섞여 나오는 상태였다). 원인은 아래 그대로였다(재확인
  완료). **수정**: `find_by_logical_identity`에 `exclude_document_id` 선택 인자를
  추가해 SQL 조회 단계에서 호출자 자신의 pending row 를 제외하도록 바꿨다(사후 `!=`
  비교 대신 조회 자체를 정확하게). `ingest_document`(`pipeline.py`)가 이 인자로
  `existing_document_id`를 넘기도록 배선. 신규 테스트 5건(저장소 레벨 3 + 파이프라인
  레벨 2, 프로덕션 순서 그대로 재현: pending row 선생성 → existing_document_id 전달)
  으로 "내용 다른 재적재 → replaced_docs_id 채워짐 → 구 docs_id 정리 호출" 과 "완전
  동일 재적재 → 여전히 skip" 둘 다 실측 확인. 기존 회귀 667 passed, 새 실패 0.
  **범위(사용자 확정)**: 이번 수정은 **신규 재업로드부터만** 적용된다 — 이미 쌓인
  중복 행/구 `docs_id`(edgequake 잔존 사본)는 이번 범위에서 정리하지 않았다(비범위,
  별건). 동시(거의 동시) 업로드 레이스는 이번 수정 이전부터 있던 별개 문제로 deferred
  유지(삭제 오류로는 안 이어짐 — `replaced_docs_id and replaced_docs_id != new_docs_id`
  가드가 보호).

  **후속(2026-08-16 밤, 같은 날 추가 수정) — 구 문서를 KB UI 목록에서도 지운다
  (kb_pipeline 한정)**: plan `~/.claude/plans/d57-followup-delete-old-doc-row.md`
  (v2 READY). D57 본수정은 예전 문서의 **검색 데이터**(edgequake docs_id)만 정리했고
  Postgres `Document` row 자체는 남아 문서 목록에 구/신 버전이 둘 다 보였다. 사용자
  지시("기존 구문서도 안 나오게 하자")로, 신규 적재 성공 후 스왑 지점에
  `deps.repo.delete_document_rows(예전_document_id)`를 추가해 `chunks_meta`+
  `document_pages`+`ingestion_jobs`+`documents` row 를 통째로 지운다(기존 문서 삭제
  API `cascade_delete_document`가 쓰는 것과 동일 삭제 경로, `SqlDocumentRepo` 에 위임
  메서드 신설). 검증 중 `SqlCascadeRepo.delete_document_rows`가 `document_pages`를
  명시 삭제 목록에서 빠뜨리고 있던 기존 결함도 같이 닫았다(모든 호출자에게 이득).
  신규 테스트로 old row/chunks_meta/document_pages 삭제 확인 + skip 케이스는 안
  지워지는지 회귀 가드. 기존 회귀 667 passed, 새 실패 0. **비-kb_pipeline provider
  (raganything/dify-edgequake/ragflow)는 각자 별도 스왑 블록이라 이번엔 안 건드림**
  (비범위, 별건).

  **후속2(2026-08-16 밤, skip 경로 orphan pending row — 수정 완료)**: plan
  `~/.claude/plans/d57-skip-path-orphan-pending-row.md`(v3 READY, ultracode 2라운드
  x2). 라이브 실측 — "소유권이전 프로세스.pptx" 재업로드가 완전 동일 내용이라 skip
  경로를 탔는데, `ingest_with_method`(`routers/kb.py`)가 적재 시작 **전에** 미리
  만들어둔 새 pending `Document` row(`existing_document_id`)가 skip 분기의 즉시
  `return` 때문에 한 번도 갱신되지 않아 **"pending/대기"로 영구히 방치**됐다(실사용자가
  UI 에서 "소유권이전 프로세스가 대기에서 멈췄다"고 보고). 급한 건은 그 1건을 라이브
  DB 에서 수동 삭제해 즉시 해소.
  **수정 v1(삭제) → 치명적 결함으로 폐기**: skip 분기에서 `delete_document_rows(
  existing_document_id)`로 지우려 했으나, `IngestionJob.document_id` FK 가
  `ondelete=CASCADE`라 **지금 이 요청을 처리 중인 잡 자신의 ingestion_jobs row 까지
  같이 지워져** `workers/tasks.py`의 후속 `jobs.set_state(...)`(try 블록 밖)가
  크래시하는 시나리오였다(원래 버그보다 더 나쁨).
  **수정 v2(상태만 canceled 로 갱신) → 새 치명적 결함 발견**: row 삭제 대신
  `deps.repo.set_status(existing_document_id, "canceled")`로 바꿔 FK 캐스케이드
  위험은 없앴으나, `find_by_logical_identity`에 상태 필터가 전혀 없어(`created_at
  DESC LIMIT 1`) canceled placeholder 가 원본 ready 문서보다 최신이라 **세 번째
  이상 동일 재업로드부터 그 canceled row 가 먼저 걸려 skip 이 깨지고 원본이 identity
  조회에서 영원히 못 찾는 orphan**이 되는 회귀를 ultracode 2라운드가 잡아냈다. 이
  poisoning 은 사실 이번 plan 이전부터 있던 잠재 버그(진짜 사용자 취소 흐름도 같은
  `set_status(..., "canceled")`를 씀, `workers/tasks.py:175,316`/`pipeline.py:2403`)임을
  추가 확인.
  **최종 수정 v3(READY, 구현됨)**: `SqlDocumentRepo.find_by_logical_identity`
  WHERE 절에 `Document.status != "canceled"` 필터를 추가해, skip 신규 사용과 기존
  취소 흐름의 잠재 버그를 한 번에 닫았다. skip 분기의 `set_status(...,"canceled")`는
  v2 그대로 유지(row 는 지우지 않음, FK 캐스케이드 위험 없음). 신규 테스트 2건
  (기존 skip 테스트에 canceled 상태 갱신 확인 추가 + 신규 "3연속 동일 업로드" 테스트
  — v3 필터가 없으면 실패하도록 설계, v2→v3 diff 의 존재 이유를 직접 검증) 로 실측
  확인. 기존 회귀 668 passed(순증 +1 테스트), 새 실패 0(기존 19건은 이 수정과 무관한
  사전 실패 — alembic/gate_options/raganything/ragflow 등).

  **원래 조사 기록(아래, 참고용)** — 레포: `99.projects/shinhan_trust/knowledge_base/backend`.
  `pipeline.py:574-577` 의 `find_by_logical_identity(kb_id, file_name)`(같은
  kb_id+파일명 중 `ORDER BY created_at DESC LIMIT 1`)가 재업로드 시 **그 요청이 방금
  만든 자기 자신의 pending Document row** 를 반환하는 경로로 보인다 — `routers/kb.py:
  upload_documents` 가 파일마다 새 `Document` row 를 먼저 커밋(`kb.py:228-239`)한 뒤
  워커가 그 row 의 id 를 `existing_document_id` 로 넘기므로(`workers/tasks.py:281`),
  그게 항상 "가장 최근" row 라 자기 자신이 걸린다. 그렇다면 `existing.document_id !=
  existing_document_id`(pipeline.py:577)가 재업로드에서 **False** 로 평가돼
  replace/skip 분기(578-591)가 발동하지 않고, 구/신 문서(및 그 `chunks_meta`)가 완전히
  독립적으로 공존할 수 있다 — 즉 같은 파일명 재업로드 시 **중복 방지 가드가 무력화**될
  가능성.
  **확정 결론 아님** — ultracode 검증 2·3라운드에서 코드 추적이 반복 번복됐고(v2: "같은
  document_id 재사용" → v3: "항상 새 id + replace 분기 항상 발동" → v3 재검증에서 그마저
  틀림), 실제 프로덕션 재현은 검증 렌즈 1건이 sqlite 인메모리로 흉내낸 것뿐이라 운영 DB
  기준 재현이 필요하다. `~/.claude/plans/phase25-lane-visibility-ui.md` v2~v4 변경이력에
  전체 추적 기록이 있다. 별도 조사·plan 필요(재현 → 실제 영향 범위 확정 → 수정 여부 결정).
