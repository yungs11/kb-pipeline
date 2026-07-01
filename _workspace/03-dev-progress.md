# 03 · 개발진행사항

> 출처: SoT.md §6/§8/§10/§11, kb-stage-monitoring.md, process-definition §6.
> 범례: ✅ 완료(merged) · ◐ 부분/하드닝 과제 · ☐ 미구현/계획.

---

## 1. 차용 vs 신규 작업항목 (W0~W6)

**차용(reuse)**: kordoc/OpenDataLoader/markitdown/VLM(파서) · raganything `modalprocessors` 구조 · `adaptive_chunk` 서비스(329 테스트) · edgequake(`migrations/*.sql` + `edgequake-pipeline`/`-storage`/`-api`).

| ID | 항목 | 상태 | 내용 |
|----|------|------|------|
| **W0** | Blockify | ✅ merged | `hybrid_to_blocks()` + VLM `elements[]` 매핑. kb-pipeline `main`, pytest 포함 |
| **W1** | ChunkingStrategy 통합 | ✅(v1) → facade 로 이전(v2) | v1: edgequake `AdaptiveChunkStrategy`(Rust) + `Pipeline::with_chunking_strategy()` + 플래그. v2: 청킹을 facade `/chunk` 가 소유하고 edgequake 는 passthrough(02-changes §2) |
| **W2** | Modal enrichment | ✅ merged | 모달 블록 LLM 서술(텍스트/비전) + atomic 인라인 + (옵션)앵커 엔티티. `kb_pipeline/modal.py` |
| **W3** | Community 배치 | ✅ merged | `kb_pipeline/community.py` — Louvain + qwen 리포트 + `global_query`. 순수 Python(edgequake 불변). 라이브: 커뮤니티 60/리포트 15 |
| **W4** | 정합성/RLS | ◐ 하드닝 과제 | 앱레벨 격리 검증됨. DB레벨 FORCE RLS 는 superuser 롤 우회로 무력(아래 §4) |
| **W5** | Search 머지 | ✅(라이브러리) / ◐(배선) | `kb_pipeline/search.py` local/global `route`. 단 facade `/search` 는 bare edgequake hybrid 만 노출, unified_search 미배선 |
| **W6** | 파서 라우팅 | ◐ 권고 반영 | markitdown 병합표 손실 → 병합 중요 pptx/DOCX 구조파서 라우팅(02-changes §4) |

---

## 2. E2E 검증 상태

**E2E 통과** — kb_pipeline frontend → edgequake → 추출 → 임베딩 → 검색 전 구간 실동작.
- 구성: LLM=OpenRouter `qwen/qwen3.5-122b-a10b`, 임베딩=`bge-m3` 1024d.
- 결과(문서 1건): chunk 12, **모달 4개 전부 단일 atomic 청크**(T1→chunk-6, T2→8, T3→9, T4→11), entity 158, relationship 111, 임베딩 147행 전부 1024d, `/api/v1/query` 검색 동작, 0 실패.

> ⚠️ 위 §11 E2E 기록은 **v1(edgequake=adaptive)** 시점이다. **현행 facade 경로는 `EDGEQUAKE_CHUNKER=passthrough`** — 청킹·모달원자성은 facade `/chunk` 가 소유, edgequake 는 1:1 passthrough 저장(02-changes §2).

**테스트(main, `.venv-kb`)**: pytest 60 passed (blockify/modal 21 + community 14 + search 25; 2026-06-19). W6 라우팅 테스트(+14)는 PR#4 브랜치.

**완료(merged)**
- W0 blockify + W2 modal: kb-pipeline `main` (pytest 21).
- W1 + `Pipeline::with_chunking_strategy()` + 팩토리 플래그: edgequake fork `edgequake-main`. cargo green, 유닛 10.
- kb-pipeline `main` 이 edgequake submodule 을 `edgequake-main` 에 핀.
- W3 community: `kb_pipeline/community.py` — AGE→Louvain(python-louvain)→커뮤니티별 qwen 리포트(GraphRAG 프롬프트 이식)→`community_reports`→`global_query` map-reduce. 라이브: 커뮤니티 60/리포트 15. edgequake Rust 불변.
- W5 unified search: `kb_pipeline/search.py` — `route`(local/global) + 워크스페이스 스코프. 라이브 2워크스페이스(휴가/담보신탁): local 정확, global map-reduce, 교차 스코핑 누출 0.

---

## 3. 타이밍 모니터링 plan (kb-stage-monitoring, Option B)

> 상태: **plan v2 READY**. facade `/chunk`·`/insert` passthrough 훅은 코드에 존재하나, P2/P3/P5/P6 는 **미구현**.

### 배경 (측정 근거)
12페이지 문서가 **파서 ~5분 / 청커 ~10분** — 서비스화 불가. 원인 확인:
- **청커 10분** = 커밋 `5b381db`(2026-06-22) 회귀. `/chunk` 텍스트 경로에 `regex_llm`/`rerank_fn`/`coref_fn` DI 주입 → 4방법 경쟁. `llm_regex.split`=reasoning LLM 단일콜 **~339s 실측**, `semantic.split`=문장쌍 N-1 reranker, coref RC LLM. **승자 1개 선택에 전부 지불**.
- **파서 5분** = 표/그림당 vision LLM(`enhance_media_sections_with_vision`/figure_parser, 표 N개×20-40s) + OpenDataLoader HTTP + TSR/회전.

**사용자 결정**: 완화는 **비범위**(4방법 경쟁 유지 — llm_regex/semantic 끄지 않음). 화면은 10분을 **드러내되 줄이진 않음**.

### 목표
파서·청커·edgequake 내부 단계별 소요시간(+파서 카운터)을 **통일 타이밍 트리**로 수집 → facade 가 sub-tree 병합 → kb-backend 영속(IngestionJob JSONB) → 프론트 **PipelineTimingMonitor 카드**(단계×소요시간×% 바/표, 임계 색상).

### 통일 타이밍 트리 계약 (척추, 단위 ms·float)
```
timings = {
  "total_ms": float,
  "stages": {
    "parse":          {"total_ms", "counters":{pages,tables,images}, "sub":[{"name","ms","calls"?}]},
    "blockify":       {"total_ms"},
    "modal_enrich":   {"total_ms", "sub":[{"name","ms","calls"}]},
    "adaptive_chunk": {"total_ms", "methods":[{"method","split_ms","score_ms","ok"}],
                       "metrics":{sc,icc,dcc,bi,rc,ba}, "extra":{gap_resplit_ms,page_attr_ms,overlap_ms,serialize_ms}},
    "edgequake":      {"total_ms", "phases":[{"name","ms"}]}
  }
}
```
누락 stage 는 생략(부분 실패/스킵 표면화).

### Phase 진행 상태
| Phase | 내용 | 상태 |
|-------|------|------|
| P0 | 배선/단계 확정 스파이크(하드 게이트): S0a parse-svc 실제 내부 실행지점, S0b 데이터경로(facade /parse→/chunk→/insert, 집계자=kb-backend `core/pipeline.py`), S0c 12p 문서 parse_method+카운터 | S0b ✅(codex 확인) · S0a ☐(하드 게이트) |
| P1 | 청커 timing surface — `service/runner.py` per-method split_ms/score_ms + per-metric `_timings` 노출(`timing_details` top-level 키), `to_public()` 자동 흐름 | 일부 완료 |
| P2 | 파서 단계 타이머 + 카운터(parse-svc `perf_counter` span, vision_enhance per-item) → `/parse` 응답 `timing_metrics` | ☐ |
| P3 | edgequake 단계 타이밍 — per-phase 타임스탬프 부재 → `/insert/status` 폴 관측 전이 시각으로 **근사**(해상도=폴 간격, 화면에 "근사" 표기) | ☐ |
| P4 | facade 가 컴포넌트 sub-tree 반환(키 추가만, 기존 응답 계약 불변) | 훅 일부 존재 |
| P5 | kb-backend 집계(`core/pipeline.py`) + 영속(`stage_timing_history` JSONB 컬럼+마이그레이션) + DTO | ☐ |
| P6 | 프론트 `PipelineTimingMonitor` 카드(`StageTiming` 바/트리, `app/kb/[kbId]/page.tsx`) | ☐ |

### plan 리스크
- parse-svc 내부 실행지점 불확정(S0a) — 자체 vs ragflow deepdoc. 게이트 차단.
- 응답 bloat / R1 계약: 청커 timing_details 가 R1 키셋 변경 → `test_service_chunk_api` 키 단언 갱신 필요. 무거운 detail 은 AC_TIMING/요청 플래그 게이트 고려.
- edgequake phase 는 타임스탬프 부재로 폴-관측 근사(정확값은 edgequake surface 신설 = 비범위).
- DB 마이그레이션(JSONB) — kb-backend 기존 적재 경로 회귀 0 필수.

---

## 4. 리스크 / 미검증 / 협의사항

### 4.1 RLS / 격리 (W4)
- RLS 정책은 documents/entities/relationships/chunks/graph 를 모두 덮으나, 앱이 Postgres **superuser 롤(`edgequake`, rolbypassrls=t)로 접속 → FORCE RLS 도 무조건 우회**(롤백 tx 로 실증). 앱레벨 격리는 검증됨, DB레벨 RLS 는 현재 무력.
- 활성화 = (비-superuser 롤+GRANT)+FORCE RLS+요청당 tx GUC(요청=단일 tx)+NULL-tenant 폴백 정리, **all-or-nothing 하드닝 과제**. 변경 미적용(known-good 유지).

### 4.2 파서 / 입력 포맷
- W6 markitdown 병합표 손실(pptx/DOCX) → 구조파서 라우팅 권고.
- HWP/HWPX/HWPML 실측 생략(수용된 리스크) — 운영 중 품질 문제 시 사후 검증.
- OCR·VLM `strategy=hybrid` 외 전략·timeout 600s 적정성, `_DIGITAL_MIN_CHARS=1` 보수값 적정성.
- Excel 백엔드(excel-parser :18055) 자동 기동·라우팅 정식화(현재 markitdown 사용).

### 4.3 Modal Enrich
- 그림(image) vision LLM 연결: 현재 `/parse` 는 `vision_llm=None` → 그림 원본 통과. vision 백엔드 확정 필요.
- `KBP_MODAL_ENRICH` 기본 off 운영 정책: 검색 품질(표 의미요약) vs 파싱 속도/프록시 부하 트레이드오프 확정 필요.
- 모달 이중추출 회피: (옵션)앵커 엔티티와 중복/충돌 안 나게 dedup 규칙 필요.

### 4.4 Chunking
- 토큰 타깃(허브 기본 1100/600) KB 정책 고정 vs 문서군별 분기.
- `semantic` 은 리랭커 호출 폭증으로 대형 문서 타임아웃 위험 → 활성 정책/폴백 기준 확정.
- 모달 원자성 시 토큰수·순서 정합 유지 검증.
- offset/line 근사: adaptive_chunk 텍스트 변형 시 lineage 정확도.

### 4.5 Insert / Community / Search 배선
- **Search 라우터 배선**: facade `/search` 는 bare edgequake hybrid 만 노출, `unified_search` local/global 미배선(app.py 미import). global 능력 노출 시점 확정 필요.
- **커뮤니티 트리거/가드**: `/communities/build`(202+백그라운드, 예외 swallow) 온디맨드 + global 검색 build-if-missing 공존. KB 규모별 admission 임계(SPEC-006 리소스 가드)가 운영 가능 형태인지 확정.
- **커뮤니티 재생성 비용/주기**(W3): 가드 임계 초과 시 거부 → KB 성장 곡선 맞춘 임계·주기 설계.
- **DSN 포트 정합**: `port=5432`(코드 기본) vs `:5433`(운영) — 환경별 명시.
- **edgequake base URL 정합**: search.py `:8080` vs 기동 `:8081` — 일원화.

### 4.6 모니터링 / 인덱스
- 타이밍 모니터링 미구현(§3) — parse-svc 실행지점 확정이 하드 게이트.
- KV GIN 인덱스: `value` 전체 GIN 금지(02-changes §3.3).

---

## 5. 비범위 (Out of Scope)

- 벡터/그래프 store 를 Qdrant/Memgraph 등으로 분리(단일 Postgres 원칙 위배, RLS·ACID 상실).
- raganything/LightRAG 를 런타임 엔진으로 채택(아이디어만 차용).
- edgequake `TaskType::Reindex` 단건 재색인(workspace rebuild 로 대체).
- 청킹 4방법 경쟁의 완화/최적화(사용자 결정으로 현상 유지).
- (가)경로(markdown 만 POST)는 MVP 폴백으로만 보존.
- 크로스-잡 집계/대시보드(Option C)·알림(추후).
