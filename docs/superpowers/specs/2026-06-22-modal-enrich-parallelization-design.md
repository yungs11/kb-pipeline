# 모달 enrich 병렬화 — 설계

**작성일:** 2026-06-22
**대상:** `kb_pipeline/modal.py::enrich` (단일 함수 리팩터)

## 1. 배경 / 문제

`enrich`는 모달(표/이미지/수식)마다 LLM을 **순차** 호출한다(한글요약 + 제목/각주 경계 판정).
표가 많은 PDF는 표 4개 ≈ 400s+ → facade `httpx.ReadTimeout`까지 유발. 호출들은 서로
독립적이므로 **동시 호출**하면 벽시계 시간이 ~1/N로 줄어든다(표 4개 ≈ 100~150s).

## 2. 난점 — 이중흡수 방지가 순차에 의존

현재 Pass 1은 모달을 문서순으로 처리하며 각 모달의 LLM 결과로 `consumed`를 갱신하고,
다음 모달의 윈도우 수집이 `consumed`를 건너뛴다(두 모달 사이 블록을 양쪽이 흡수하는 것
방지). 즉 **윈도우 수집이 직전 모달의 LLM 결과에 의존** → 단순 병렬화 불가.

## 3. 해법 — 3-phase 분리 (LLM만 병렬)

LLM 판정을 **최대 윈도우**(consumed 무시)로 독립 수행하고, 충돌은 **사후 순차 패스**에서
결정적으로 해소한다. 결과는 기존 순차 알고리즘과 **동일**하다.

- **Phase A — 수집/검증 (순차, LLM 없음):** blocks를 문서순 1회 순회.
  - 모달 블록마다 id 부여(T/E/I 카운터, 문서순), body 추출.
  - **최대 윈도우** 수집: `_gather_before_window(blocks, i, set())` /
    `_gather_after_window(blocks, i, set())` (consumed=빈集合 → 비-text/문서끝까지 최대).
  - **None 콜러블 검증**(여기서 raise, 기존 메시지 보존): table/equation인데 `text_llm is
    None` → `ValueError("table|equation block encountered but text_llm is None; …")`;
    image인데 `vision_llm is None` → `ValueError("image block encountered but vision_llm
    is None; …")`. (LLM 작업 시작 전 raise = 기존 동작과 동일.)
- **Phase B — LLM 병렬:** `concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX, n_modals))`
  로 모달별 호출을 `ex.map`(입력순 보존). 각 호출은
  `_parse_boundary_response(raw, len(before), len(after)) -> (summary, tc, fc)`.
  - 스레드 안전성: `service/llm.py`는 호출마다 `httpx.post`(요청별 임시 클라이언트) → 안전.
    테스트 mock은 순수 함수/배리어 → 안전.
- **Phase C — 충돌 해소 (순차, LLM 없음):** 모달을 **문서순**으로 돌며 `consumed` 유지.
  각 모달의 title/footnote를 "모달에서 **연속**, 이미 consumed면 거기서 중단"으로 클램프:
  ```
  title_idxs = []
  for idx, _ in before[:tc]:        # before = nearest-first
      if idx in consumed: break
      title_idxs.append(idx)
  footnote_idxs = []
  for idx, _ in after[:fc]:
      if idx in consumed: break
      footnote_idxs.append(idx)
  consumed |= set(title_idxs) | set(footnote_idxs)
  ```
  → 두 모달 사이 블록은 **앞 모달이 선점**(문서순 먼저 처리). 뒤 모달의 제목 윈도우는 그
  블록이 consumed라 거기서 끊김 = **기존 순차와 동일 귀결**.
- **Phase D — 출력:** 기존 Pass 2와 동일. consumed면 건너뛰고, 모달은
  `_wrap(... title=문서순 join, footnote=문서순 join)`, 텍스트는 원문. `"\n\n".join`.

## 4. 동치성 — 무엇이 보장되고 무엇이 안 되는가 (정직한 논증)

병렬판은 후속 모달의 LLM 프롬프트가 **최대 윈도우**를 보므로, 순차판(=consumed로 좁혀진
윈도우)과 **프롬프트 컨텍스트가 다르다**. 따라서 구분한다.

**(가-1) 흡수 집합 동일 — count가 윈도우에 무관한 LLM이면 흡수된 title/footnote 집합 동일.**
귀납 증명:
- 가장 앞 모달은 순차/병렬 모두 consumed=∅ → 같은 윈도우 → 같은 count → 같은 흡수.
- count가 윈도우에 무관하면 각 모달의 count는 두 경로에서 동일. 순차의 before-window는
  "첫 consumed 전까지의 연속 미소비 블록"이고, 병렬은 최대 윈도우를 Phase C가 nearest-first
  walk하며 첫 consumed에서 중단한다. `tc ≤ 미소비길이`면 둘 다 같은 `tc`개, `tc > 미소비
  길이`면 둘 다 미소비 전부를 흡수 → **동일 집합**. consumed가 동일하게 자라므로 귀납 성립.

**(가-2) byte-identical 출력 — 추가로 LLM "응답"(요약 포함)이 두 경로에서 동일해야 성립.**
(가-1)은 *흡수 배정*만 보장한다. 최종 문자열까지 동일하려면 각 모달의 LLM 응답(summary)도
같아야 하고, summary가 윈도우(payload)에 의존하면 윈도우가 좁혀진 후속 모달에서 달라질 수
있다. **본 테스트 스위트는 (가-2)를 충족한다**:
- `_json_llm` 류 mock은 prompt/payload를 **무시하고 고정 summary+count** 반환 → 윈도우가
  달라도 응답 동일(예: `test_between_block_not_double_claimed`의 T2는 윈도우가 순차=[]·
  병렬=[MID]로 다르지만 summary="s" 고정 → wrap 동일).
- `test_modal.py`의 payload-민감 mock(`DESC[payload[:10]]`)은 **비-JSON → fallback tc=fc=0
  → consumption 없음 → 후속 윈도우가 절대 안 좁혀짐 → payload 동일 → 응답 동일**.
- ⇒ 결정적 mock 테스트 36개 전부 **byte-identical**로 통과.

**(나) 보장 안 됨 — 실제(윈도우 민감·비결정) LLM은 경계 판정이 미세히 달라질 수 있다.**
후속 모달이 (Phase C에서 앞 모달에 뺏길 수도 있는) 블록까지 프롬프트에서 보므로 그 모달의
요약·count가 순차 실행과 다를 수 있다. 그래도 **Phase C가 이중흡수 금지·연속성·앞모달 우선을
항상 보장**하므로 결과는 언제나 유효(valid)하다. 실제 LLM은 동일 입력에도 run-to-run 으로
출력이 변하므로, 이 차이는 **기존 비결정성 범위 내**이며 성능 이득을 위해 수용한다.

요약: **결정적(mock) 경로 = 엄밀 동일**(테스트로 고정), **실제 LLM 경로 = 불변식 보존하의
유효한 변형**(엄밀 동일은 주장하지 않음).

## 5. 시그니처 / 계약

- `enrich(blocks, *, text_llm, vision_llm, max_workers: int = 8) -> (content, modal_ids)`.
  `max_workers`는 **추가 키워드(기본 8)** — 기존 호출부(parse_service) 무수정 동작.
- 모달 id 문서순, modal_ids 반환 순서 불변. `_wrap`/`_boundary_*`/`_parse_boundary_response`/
  윈도우 헬퍼/`_MODAL_RE`/n_blocks/청커 **무변경**.

## 6. 테스트

- 기존 `tests/test_modal*.py` 36개 전부 green(동치성 고정).
- 신규(동시성 증명): `threading.Barrier(parties=n_modals)`에서 블록하는 mock LLM →
  병렬이면 배리어 해제로 완료, 순차면 데드락 → **짧은 타임아웃 내 완료**를 단언.
- 신규(순서): 다수 모달에서 modal_ids가 문서순.
- 신규(동치 회귀): 두 표 사이 블록이 앞 표로만 흡수(병렬 경로에서도) 재확인.

## 7. Non-goals

- 프롬프트/래핑/파서/윈도우/청커/계약 변경 없음.
- async/await 전환 없음(스레드풀로 충분, 호출부 sync 유지).
- parse-svc를 async job으로 바꾸지 않음(별도 후속).
