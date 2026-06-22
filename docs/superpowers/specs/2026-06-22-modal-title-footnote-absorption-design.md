# MODAL 제목·각주 흡수 + 한글 요약 — 설계

**작성일:** 2026-06-22
**대상 모듈:** `kb_pipeline/modal.py` (+ `parse_service/app.py` 계약 검증, 테스트)

## 1. 배경 / 문제

파서의 모달 enrichment(`kb_pipeline/modal.py::enrich`)는 표/이미지/수식 블록을
`〈MODAL id type〉{요약}\n{payload}〈/MODAL〉` 한 덩어리로 감싼다. 그러나 그 표의
**제목/캡션**(모달 직전 text 블록)과 **각주/설명**(모달 직후 text 블록)은 모달
**밖**에 별도 text로 남는다. 그 결과:

- 청킹 시 제목·각주가 표 본문과 다른 청크로 떨어질 수 있다.
- 검색 시 "표 제목"으로 들어온 질의가 표 본문 청크와 매칭되지 않는다.

또한 현재 요약 프롬프트가 영어라 요약문이 영어로 생성된다.

### 목표 (사용자 확정 예시)

```
〈MODAL id="T1" type="table"〉
가정의례와 관련된 청원휴가 허가기준          ← 제목 (원문 그대로)
(개정 2025.09.01.)                          ← 부제 (원문 그대로)
[한국어 요약]                                ← LLM, 한글
<table>…</table>                            ← payload (원문 그대로)
각 대상에 대해 당해 휴가는 1회만 부여한다.    ← 각주 (마커 없음, 원문 그대로)
** 사망 시의 휴가부여는 …                     ← 각주 (원문 그대로)
*** "2. 회갑"을 대신하여 …                    ← 각주 (원문 그대로)
〈/MODAL〉
```

제목·각주 **텍스트는 원문 그대로 보존**(LLM 재작성 금지). LLM은 **요약문만** 한글 생성.

## 2. 아키텍처 결정 (확정)

**Philosophy A — 파서가 원자성(atomicity)을 소유, 청커는 산문 분할에 집중.**

- "무엇이 쪼갤 수 없는 단위인가"(표·이미지·수식 + 그 제목/각주)는 전적으로
  **파서/MODAL**이 선언한다. 제목·각주를 MODAL 안으로 흡수하면 atomic 단위가
  커질 뿐이며, 청커(adaptive_chunk)는 `〈MODAL…〈/MODAL〉` span을 불투명한 원자로
  그대로 보존한다(변경 불필요).
- 하드 보장("절대 안 잘림")은 MODAL marker가 책임지고, 청커의 BI 지표는
  *산문 구조* 보호로 자연 축소된다. 본 작업은 이 분리를 강화한다.

**경계 판정 = LLM 보조 (확정).** 표마다 이미 도는 LLM 호출 1회를 확장해,
(a) 한국어 요약과 (b) 주변 후보 줄 중 **제목 개수/각주 개수**를 판정시킨다.
마커 없는 각주("각 대상에 대해…")도 LLM 판단으로 잡는다. 텍스트는 원문 보존.

## 3. 메커니즘

### 3.1 데이터 흐름 (enrich 2-pass)

`enrich(blocks, *, text_llm, vision_llm)` 시그니처는 **유지**한다. 내부를 2-pass로
재구성한다.

- **Pass 1 (결정):** blocks를 문서 순서로 순회. 모달 블록 i를 만나면:
  1. **앞 후보 윈도우**: i-1부터 역방향으로 연속된 `text` 블록을 최대
     `BEFORE_WINDOW(=3)`개 수집(비-text 블록 또는 이미 consumed 블록을 만나면 중단).
  2. **뒤 후보 윈도우**: i+1부터 정방향으로 연속된 `text` 블록을 최대
     `AFTER_WINDOW(=6)`개 수집(비-text 또는 consumed 만나면 중단).
  3. 모달 type별 프롬프트 + 후보 윈도우 + payload를 LLM에 전달 → `{summary,
     title_count, footnote_count}` 수신(JSON). `title_count`는 표에서 가까운
     순서로 앞 후보 중 제목인 개수, `footnote_count`는 뒤 후보 중 각주인 개수.
  4. `title_count`/`footnote_count`를 윈도우 크기로 **clamp**, 음수는 0으로.
  5. 흡수 블록 인덱스 집합 = `{i-title_count … i-1} ∪ {i+1 … i+footnote_count}`을
     `consumed`에 추가. 모달 i의 결정(`title_idxs, summary, footnote_idxs,
     modal_id, modal_type, payload`)을 기록.
  - 모달 id 카운터(T/E/I)는 문서 순서로 증가(기존과 동일).
  - 문서 순서로 처리 + consumed 마킹 → 두 모달 사이의 블록이 앞 모달의 각주로
    먹히면 뒤 모달의 제목 윈도우는 그 블록을 건너뛴다(이중 흡수 방지).
- **Pass 2 (출력):** blocks를 순서대로 순회하며 segment 리스트 구성:
  - `consumed`에 든 인덱스(=흡수된 text) → **건너뜀**.
  - `text`이고 not consumed → 원문 text를 segment로.
  - 모달 블록 → Pass 1 결정으로 wrap 생성(아래 3.3).
  - `enriched_content = "\n\n".join(segments)` (기존과 동일).

### 3.2 LLM 호출 계약 (시그니처 무변경)

`text_llm(prompt, payload) -> str`, `vision_llm(img_path, prompt) -> str` 그대로.
enrich가 **JSON을 요구하는 프롬프트**를 만들고 응답을 파싱한다.

- `payload`(text 모달)에는 후보 윈도우 + 본문을 구조화해 싣는다:
  ```
  [앞 후보 — 표에서 가까운 순, B1이 가장 가까움]
  B1: (개정 2025.09.01.)
  B2: 가정의례와 관련된 청원휴가 허가기준

  [본문]
  <table>…</table>

  [뒤 후보 — 표에서 가까운 순, A1이 가장 가까움]
  A1: 각 대상에 대해 당해 휴가는 1회만 부여한다.
  A2: ** 사망 시의 …
  A3: *** "2. 회갑" …
  ```
- 프롬프트(한국어) 핵심 지시:
  - 본문을 검색용으로 **반드시 한국어로** 요약(`summary`).
  - 앞 후보 중 이 본문의 **제목/캡션**인 줄 수(`title_count`, 표에서 가까운
    연속 개수, 0..앞개수), 뒤 후보 중 **각주/설명**인 줄 수(`footnote_count`,
    0..뒤개수). 무관한 줄은 제외.
  - **오직 JSON만 출력**: `{"summary": "...", "title_count": N, "footnote_count": M}`.
- 이미지 모달은 `vision_llm(img_path, prompt)` 사용. 동일 JSON 계약(후보 윈도우는
  프롬프트 텍스트에 포함). 라이브 경로는 `vision_llm=None`이라 이미지 블록은
  기존대로 `ValueError`(변경 없음).

### 3.3 wrap 포맷 (하위호환)

`_wrap`를 확장: segment 리스트 `[title?, summary, payload, footnote?]`를 `"\n"`으로
join하고 open marker + close로 감싼다.

- **제목/각주 없음**: `〈MODAL …〉{summary}\n{payload}〈/MODAL〉` — **현재와 byte 동일**.
- **있음**: `〈MODAL …〉{title}\n{summary}\n{payload}\n{footnote}〈/MODAL〉`.
- `title`은 흡수 블록을 **문서 순서**로 `"\n"` join(원문 그대로). `footnote`도 동일.

### 3.4 파싱 + fallback (하위호환의 핵심)

`_parse_boundary_response(raw, n_before, n_after) -> (summary, title_count,
footnote_count)`:

- ```json``` 코드펜스 제거 후 첫 `{...}` JSON 파싱.
- 성공: `summary`=문자열, `title_count`/`footnote_count`=정수로 clamp(0..n_before/n_after).
- 실패(비-JSON, 키 누락 등): `summary=raw.strip()`, `title_count=0`, `footnote_count=0`.
  → 흡수 0건, 요약=원문 응답. **기존 동작과 동등** → `tests/test_modal.py` 기존 10개 무변경 통과.
  (단, 첫 유효 JSON 객체는 `json.JSONDecoder().raw_decode` 로 파싱해 코드펜스/후행 잡음/
  바깥 중괄호에 안전하게 — greedy 정규식 미사용.)

## 4. 청커/계약 영향

- **adaptive_chunk**: 변경 없음. `atomic_markers=[("〈MODAL","〈/MODAL〉")]`가 확장된
  span을 통째 원자 보존.
- **modal_spans**(`parse_service/app.py::_MODAL_RE`): 변경 없음. `.*?`(DOTALL)가
  제목·각주 포함 확장 span을 open~close로 정확히 캡처. 제목/각주 텍스트에
  `〈/MODAL〉`(U+3008 시퀀스)가 들어갈 일 없어 안전. char_range가 확장 span 전체를
  가리키는지 테스트로 검증.
- **n_blocks**: `len(blocks)` 그대로(원시 블록 수). 흡수는 출력 segment에서만 제외.

## 5. 엣지 케이스 (테스트로 커버)

1. 제목 2 + 마커없는 각주 포함 각주 3 흡수 → 단일 span, 외부 중복 0.
2. 주변 text 없음 → 현재와 동일(하위호환).
3. `title_count`가 윈도우 초과 → clamp.
4. 비-JSON 응답 → fallback(0/0), crash 없음, 흡수 없음.
5. 인접 두 표가 사이 블록 공유 → 앞 표가 각주로 선점, 뒤 표 제목 윈도우는 건너뜀.
6. `title_count=0, footnote_count=2` → 각주만 흡수.
7. 흡수된 text는 enriched_content에 **정확히 1회**(span 안)만 등장.
8. 프롬프트에 한국어 요약 지시 문자열 포함(요약 한글화 검증).

## 6. Non-goals

- 청커(adaptive_chunk) 코드 변경 없음(A 철학).
- 이미지 vision 경로의 라이브 활성화(여전히 `vision_llm=None`).
- 제목/각주 텍스트의 LLM 재작성(원문 보존만).
- BI 지표/메서드 선택 로직 변경 없음.

## 7. 테스트 전략

- `tests/test_modal.py`: 기존 10개 유지 + 신규(흡수/clamp/fallback/이중흡수/중복0/
  한글지시/순서). LLM은 mock(JSON 반환 / 비-JSON 반환 양쪽).
- `parse_service/tests/test_parse.py`: modal_spans가 확장 span을 정확히 캡처.
- 순수 헬퍼(`_gather_before/after_window`, `_parse_boundary_response`,
  `_build_boundary_payload`, 확장 `_wrap`)는 LLM 없이 단위 테스트.
