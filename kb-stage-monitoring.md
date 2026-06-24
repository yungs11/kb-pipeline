<!-- plan-version: v2 -->
<!-- codex-validation: READY v2 at 2026-06-24T00:11:51Z -->


# kb-pipeline 단계별 타이밍 모니터링 화면 (Option B)

## 배경 / 동기 (측정 근거)
12페이지 문서가 **파서 ~5분 / 청커 ~10분** — 테스트 땐 없던 현상, 서비스화 불가. 스카우트로 원인 확인:
- **청커 10분 = 커밋 `5b381db`(2026-06-22) 회귀**: `/chunk`·`/chunk/jobs` 텍스트 경로에 `regex_llm`/`rerank_fn`/`coref_fn` DI 주입 → 그 전엔 recursive_1100/600 2개만(빨랐음), 이후 4방법 경쟁. `llm_regex.split`=reasoning LLM 단일콜 **~339s 실측**, `semantic.split`=문장쌍 N-1 reranker, coref RC LLM. **승자 1개 선택에 전부 지불**.
- **파서 5분 = 표/그림당 vision LLM**(`enhance_media_sections_with_vision`/figure_parser, 표 N개×20-40s) + OpenDataLoader HTTP + TSR/회전. 지배 요인은 **계측으로 가려야** 함.

원인은 이미 알지만(특히 청커), **사용자 요구 = 파서·청커·edgequake 내부를 단계별로 잘게 쪼개 각 구간 소요시간을 보는 모니터링 화면**(Option B). **완화는 비범위**(사용자 결정: 4방법 경쟁 현상 유지 — llm_regex/semantic 끄지 않음).

## 목표
1. 파서·청커·edgequake **내부 단계별** 소요시간(+파서 카운터 num_pages/tables/images)을 계측해 **통일된 타이밍 트리**로 수집.
2. 오케스트레이터(8.kb-pipeline/service)가 집계자 — 각 다운스트림이 반환한 sub-stage 트리를 wall-clock으로 감싸 병합.
3. knowledge_base가 폴링·영속(IngestionJob JSONB) → 프론트 **PipelineTimingMonitor 카드**(단계×소요시간×% 바/표, 임계 색상).

## 비목표
- **완화/최적화 없음**(사용자 결정): llm_regex/semantic/coref 그대로 둔다. (단, 청커 텍스트경로가 4방법인 것은 화면이 드러내기만 함.)
- 크로스-잡 집계/대시보드(Option C)·알림은 비범위(추후).
- 파서 알고리즘 변경 없음(계측만 추가, 동작 불변).

## 통일 타이밍 트리 계약 (이 기능의 척추)
모든 컴포넌트가 이 모양의 부분트리를 반환/누적한다(단위 ms):
```
timings = {
  "total_ms": float,
  "stages": {
    "parse":          {"total_ms", "counters": {pages, tables, images}, "sub": [{"name","ms","calls"?}]},
    "blockify":       {"total_ms"},
    "modal_enrich":   {"total_ms", "sub": [{"name","ms","calls"}]},
    "adaptive_chunk": {"total_ms", "methods": [{"method","split_ms","score_ms","ok"}],
                       "metrics": {"sc","icc","dcc","bi","rc","ba"}, "extra": {"gap_resplit_ms","page_attr_ms","overlap_ms","serialize_ms"}},
    "edgequake":      {"total_ms", "phases": [{"name","ms"}]}
  }
}
```
누락 stage 는 생략(부분 실패/스킵 표면화). 단위는 전부 ms·float.

## Phase 0 — 배선/단계 확정 스파이크 (하드 게이트, 추측 금지)
- **S0a (parse-svc 실제 내부)**: `8.kb-pipeline/parse_service/app.py`(:19001 /parse)가 파서 단계를 **자체 구현**하는지 **ragflow deepdoc**(`deepdoc/parser/opendataloader_parser.py`·`pdf_parser.py`·`figure_parser.py`)를 호출하는지, 표/그림 vision LLM 이 어디서 도는지 라이브/코드로 확정. → 파서 타이머를 **실제 실행 지점**에 박는다.
- **S0b (데이터 경로 — codex v1 확인됨)**: knowledge_base `backend/app/clients/kb_pipeline_client.py`·`core/pipeline.py` 가 kb-pipeline **facade `/parse` → `/chunk` → `/insert`(+poll `/insert/status`)** 를 순차 호출한다(one-shot `/ingest` 아님). `core/pipeline.py` 가 IngestionJob stage 를 추적하는 **집계자**. 확정할 것: pipeline.py 가 이미 기록하는 **정확한 stage 이름**(트리를 거기에 맞춤) + IngestionJob 영속 지점(`models/ingestion_job.py`·`repositories.py`·`workers/tasks.py`).
- **S0c (12p 문서 parse_method + 카운터)**: 그 문서의 parse_method(OpenDataLoader/DeepDOC/Vision)와 표/그림/페이지 수 — **카운터부터** 넣어 vision-LLM vs OCR/TSR 지배를 데이터로 가른다(사용자 선택).
- **게이트**: 위 3개를 `8.kb-pipeline/docs/` 또는 `_workspace`에 확정 기록. 모호하면 다음 Phase 보류.

## Phase 1 — 청커 타이밍 surface (최저 노력·즉시 가치; 일부 완료)
`adaptive_chunk` 는 이미 per-method `split_ms`/`score_ms`(MethodOutcome) + per-metric `_timings`(aggregate)를 **계측 중**. 노출만 한다:
- `service/runner.py run_chunk()`: `result.outcomes`·`result.scores[selected]['_timings']` 로 `timing_details` 블록(위 계약의 `adaptive_chunk` 모양 + `gap_resplit_ms/page_attr_ms/overlap_ms/serialize_ms` per-counter span 신설)을 **양 return dict 의 새 top-level 키**로 추가. **scores 안에 넣지 않음**(test_service_chunk_api 의 "no `_`-prefix"·R1 키 계약 보존 — 새 키는 R1_KEYS 와 별개라 그 테스트들은 갱신 필요 → 명시).
- `service/jobs.py to_public()`: 이미 result dict 를 그대로 노출 → `timing_details` 가 `GET /chunk/jobs/{id}` 로 자동 흐름(스키마 변경 0).
- `service/main.py`: `logging.getLogger("service.runner").setLevel(INFO)`(또는 `_emit_timing_log` 를 stderr 로) → uvicorn 에서 ac_timing 가시(현 빈틈 수정).
- 테스트: `tests/test_service_chunk_api.py` 에 `timing_details` 키 존재·모양 단언 추가(R1 키 계약은 새 키 포함하도록 갱신). 전체 스위트 회귀 0.

## Phase 2 — 파서 단계 타이머 + 카운터 (parse-svc)
S0a 결과의 실제 실행 지점(8.kb-pipeline/parse_service 또는 ragflow deepdoc)에 `perf_counter` span:
- opendataloader_http, opendataloader_response_parse, layout, OCR loop, TSR+auto-rotate, **vision_enhance_PER_ITEM**(표/그림 LLM, `num_calls`/표당 시간 포함), output_format.
- 카운터: num_pages/num_tables/num_images + vision LLM `num_calls`/tokens(가능 시).
- `/parse` 응답에 `timing_metrics`(위 계약 `parse` 모양) 추가 → `service/parse_client.py` 가 수신해 반환.
- 동작 불변(계측만), 파서 단위테스트 회귀 0.

## Phase 3 — edgequake 단계 타이밍 (근사, poll-derived — codex v1 #2)
edgequake `GET /api/v1/documents/{id}` 는 `raw_status`/`phase`/`chunk_count`/`terminal`/`succeeded` 만 반환 — **per-phase 타임스탬프가 없다**(codex 확인). 따라서 phase 소요는 **집계자(knowledge_base pipeline.py)가 `/insert/status` 를 폴링하며 관측한 phase 전이 시각**으로 도출한다(해상도 = 폴 간격, **근사값**임을 화면에 명시 — 성공 위장 금지). edgequake stage = `total_ms`(insert 호출~terminal) + 관측 phase(chunking/extracting/embedding/storing) 근사 sub. **정확한 phase 타임스탬프는 edgequake 신규 surface 필요 → 비범위(선택, 추후 plan).**

## Phase 4 — kb-pipeline facade 가 컴포넌트 sub-tree 반환 (8.kb-pipeline/service)
knowledge_base 는 kb-pipeline **facade `/parse` → `/chunk` → `/insert`(+poll `/insert/status`)** 를 순차 호출(codex 확인). 각 facade 핸들러가 다운스트림 호출을 wall-clock 으로 감싸 **자기 컴포넌트 sub-tree 를 응답 dict 에 추가**(키 추가만, 기존 응답 계약 불변):
- `/parse`(→ parse_service): 응답에 `timing_metrics`(P2: parse stage + counters) 포함. (`service/parse_client.py`·`parse_service` 연계.)
- `/chunk`(→ adaptive_chunk job): result 의 `timing_details`(P1: methods/metrics/extra) 를 facade 응답으로 패스스루. (`service/adaptive_chunk.py`.)
- `/insert`·`/insert/status`(→ edgequake): `total_ms` + 관측 phase(P3 근사) 포함. (`service/edgequake.py`.)
- one-shot `/ingest`(`service/app.py`+`ingest.py run_front`: parse/blockify/modal_enrich)도 동일 트리로 갱신(일관성, 선택). blockify/modal_enrich sub-timer 는 `kb_pipeline/blockify.py`·`modal.py` 호출 지점에 추가.

## Phase 5 — knowledge_base 집계 + 영속 + API (집계자 = core/pipeline.py)
UI 의 집계자는 **knowledge_base `backend/app/core/pipeline.py`** — 이미 `/parse`→`/chunk`→`/insert` 를 순차 호출하고 IngestionJob stage 를 추적(codex 확인). 여기에:
- 각 facade 호출을 `monotonic` 으로 감싸 stage `total_ms` + facade 가 준 sub-tree(`timing_metrics`/`timing_details`/edgequake phase)를 병합 → **통일 타이밍 트리**(위 계약, S0b 의 실제 stage 이름에 정렬) 구성.
- `models/ingestion_job.py`: `stage_timing_history` **JSONB** 컬럼 + 마이그레이션(깊은 트리 유연).
- `clients/kb_pipeline_client.py`: facade 응답에서 sub-tree 추출해 pipeline 으로 전달.
- `repositories.py`/`workers/tasks.py`: set_state/완료 시 트리 기록.
- `routers/jobs.py`: JobStatus DTO 에 중첩 timing 트리 + `getJobTiming()`.
- **knowledge_base 기존 적재 경로 회귀 0**(마이그레이션 포함).

## Phase 6 — knowledge_base 프론트 PipelineTimingMonitor 카드
- `frontend/lib/types.ts`: 중첩 timing 트리 타입. `lib/api.ts`: `getJobTiming()`.
- 신규 `StageTiming`(수평 바 또는 표: stage|sub|ms|% , 임계 색상 `--ok/--err/--accent`) + `PipelineTimingMonitor`(기존 `JobList.tsx` 폴링 패턴 재사용) → `app/kb/[kbId]/page.tsx` 그리드 카드. **파서/청커/edgequake 각 내부 단계가 펼쳐지는 트리**(사용자 요구).

## 검증 게이트(Phase별)
- P0: S0a/b/c 확정 기록(미확정 시 보류).
- P1: 청커 timing_details 가 job result 로 흐름 + 테스트 green + 전체 스위트 회귀 0 + uvicorn ac_timing 가시.
- P2: /parse 응답 timing_metrics + 카운터, 파서 동작 불변(회귀 0).
- P3: edgequake per-phase 도출.
- P4: facade `/parse`·`/chunk`·`/insert` 응답에 각 컴포넌트 sub-tree 포함(키 추가만, 기존 응답 계약 불변) — 라이브 1건서 확인.
- P5: core/pipeline.py 가 통일 트리로 병합 + IngestionJob JSONB 영속 + DTO + 마이그레이션, knowledge_base 기존 테스트 회귀 0. 라이브 1건서 parse/chunk/edgequake stage 모두 채워짐.
- P6: 카드가 파서·청커·edgequake **내부 단계별** 바/트리를 라이브 잡에서 렌더(E2E 1건).

## 리스크 / 미해결
- **parse-svc 내부 위치 불확정**(S0a) — 자체 vs ragflow deepdoc. 게이트가 차단.
- **응답 bloat / R1 계약**: 청커 timing_details 가 R1 키셋을 바꾸므로 test_service_chunk_api 키 단언 갱신 필요(명시). 무거운 detail 은 AC_TIMING/요청 플래그 게이트 고려.
- **타이밍 부분성**: pipeline.py 가 `/parse`→`/chunk`→`/insert` 를 순차 진행하며 stage 가 끝나는 대로 트리를 채움 → UI 는 미완 stage 를 "진행중/측정 전" 으로 표면화(성공 위장 금지).
- **edgequake phase 는 타임스탬프 부재로 폴-관측 근사**(해상도=폴 간격, codex v1 #2) — 화면에 "근사" 표기. 정확값은 edgequake surface 신설(비범위).
- **경로 계약 확정(codex v1 #1)**: 집계는 `/ingest` 가 아니라 facade `/parse`→`/chunk`→`/insert`(+`/insert/status`), 집계자=knowledge_base `core/pipeline.py`.
- 완화 비범위라 화면은 10분을 **드러내되 줄이진 않음**(사용자 결정) — 화면이 정당성 증빙 후 별도 완화 plan 가능.
- DB 마이그레이션(JSONB) — knowledge_base 기존 적재 경로 회귀 0 필수.
