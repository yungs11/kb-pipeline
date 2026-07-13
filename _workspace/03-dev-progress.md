# 03 · 개발진행사항

> 출처: SoT.md §6/§8/§10/§11, kb-stage-monitoring.md, process-definition §6.
> 범례: ✅ 완료(merged) · ◐ 부분/하드닝 과제 · ☐ 미구현/계획.

---

## 0. 그룹 기반 KB 접근제어 (구현 완료·미머지, 2026-07-04)

설계·결정: 02-changes §0-B, 01-architecture §6. 계획: `docs/superpowers/plans/2026-07-04-group-based-kb-access-control.md`
(v4 READY, adversarial 3라운드). 구현: ultracode 서브에이전트 워크플로(12태스크 TDD+대립리뷰, 전부 통과) → 브랜치 `feat/group-access-control`(kb-backend), commits 12+1.

- **완료(kb-backend)**: `Group`/`GroupMember`(user↔group M:N) + `knowledge_bases.group_id` FK(`ON DELETE SET NULL`, KB↔group 1:1) + Alembic 마이그레이션(백필 포함, **저작만·미apply**). `acl.can_read_kb`=그룹 멤버십만(owner=관리권한만), 개별 `kb_shares` **410 은퇴**(테이블 dormant). deps + agents/comparison/jobs 호출부 `SqlGroupMembersRepo` 로 이관(grep 증명). `/groups` CRUD+멤버십(developer 게이트). `POST /kb` `group_id`(**필수**, 2026-07-07 확정: 그룹을 먼저 만든 뒤 그 group_id 로 KB 생성 — 자동 기본그룹 생성·미매핑(NULL) 생성 **모두 금지**. 미지정→422, 존재하지 않는 group_id→422), `GET /kb` 멤버십 필터. **프론트 UI(2026-07-07)**: `/kb` 생성 폼에서 "지점 코드(1~5)" 필드를 **"할당 그룹" 드롭다운(필수, `listGroups()`)** 으로 교체(branch_code 는 기본 1 전송, dify 적재용). 목록 배지도 "지점 N" → **그룹명** 표시(`KbSummary` 에 `group_code`/`group_name` 추가). ProviderBadge 는 **kb_pipeline 케이스 추가 + 미상 provider 를 dify 로 오표기하지 않도록** 수정(provider 별 정확 배지). `GET /groups` 는 일반 사용자도 조회 허용(생성/삭제/멤버는 developer 유지). ※ 필수화로 group_id 없이 POST /kb 하던 기존 테스트들은 conftest `_make_autogrant_client`(그룹 auto-provision + owner grant)로 전제 제공; 필수성 계약은 `test_create_kb_requires_group_id`(raw client, 422)로 검증.
- **완료(facade, kb-pipeline)**: `X-Facade-Key` 공유시크릿 게이트(stateful 엔드포인트) — kb-backend 만 호출, 우회 차단. facade pytest 45/45 green.
- **검증**: kb-backend `577 passed, 11 failed` — 11개는 **전부 pre-existing docguard 게이트/파이프라인 실패**(baseline 동일: test_job_status 5 + test_pipeline 4 + raganything 1 + ragflow 1). 그룹기능 전용·연관 테스트는 100% green. `SqlSharesRepo`→acl dangling 0(grep).
- **Phase 0 Postgres 통합 ✅ 완료(2026-07-06)**: 실제 배포가 **compose(`kbp-postgres-1`, 볼륨 이미 영속)** 임을 확인 → 플랜의 standalone 런처 볼륨화(Task 0.1)는 불필요로 스킵. 실상은 "두 PG 서버 통합"이 아니라 **kb-backend 를 sqlite(dev.db 18MB) → 같은 인스턴스의 `kb_orchestrator` DB 로 이전**. 절차: `kb` 롤+DB 생성(비파괴) → `alembic upgrade head`(신스키마) → **스냅샷 기반 데이터 복사**(11테이블 카운트 전부 일치: KB 23/docs 145/chunks 10,747/jobs 176/…) + **KB별 기본그룹 백필**(group_id NULL 0) + **기존 kb_shares 2건을 그룹 멤버십으로 보존** → config/.env pg 전환(백업+롤백주석) → **kb-backend 재기동(feature 코드+pg)**. 검증: 앱이 kb_orchestrator 연결(pg_stat_activity), `/groups` 401 게이트 라이브, compose 스택 8개 healthy 무손상. 이관 스냅샷 `_workspace/dev.db.migrate-snapshot-20260706` 보존(롤백 자산).
- **미완/후속**: ☐ 브랜치 `feat/group-access-control` **머지**(코드는 재기동으로 dev 라이브 배포됨, 정식 머지·PR 미완). ☐ 프론트(그룹 생성/지정 UI). ◐ W4 RLS 하드닝(별도, 그룹기능과 직교). ☐ pre-existing 게이트 11실패는 본 작업 무관(별도 조사). ☐ facade 시크릿게이트 활성화(`KBP_FACADE_KEY` 미설정=현재 게이트 dormant, facade 컨테이너도 구이미지 — 활성화 시 facade 재배포+양쪽 키 설정 필요).

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

## Phase 2 파서 일원화 진행 (2026-07-03)

plan: `docs/superpowers/plans/2026-07-02-parser-consolidation-phase2.md` (v3 READY, ultracode 2라운드 검증) · spec: `docs/superpowers/specs/2026-07-02-parser-consolidation-phase2-design.md`

| Phase | 내용 | 상태 |
|---|---|---|
| **2a** | parse-svc 재구조화(parsers/{pdf,ocr,excel}+tools+router) + `chunk_needed` flag + facade /ingest 분기 (excel/ocr 는 HTTP 위임 유지 — 동작 보존) | ✅ 완료 (Task1~7, `51692e9`..`2144f00`) — 전체 200 passed(기존 무관 실패 1: minio bucket auto-create 드리프트), 스택 스모크 green(xlsx chunk_needed=false 자체청킹 / md ingest indexed) |
| **2b** | excel_parser_rag in-process 흡수 (HTTP 제거) | ✅ 완료 (Task8~9, `f59a40b`+`8cfeb05`) — 패키지 vendoring(자기참조 import 0건, 상대임포트만), `_fetch_rag_chunks` in-process(get_backend(cfg.backend).parse), excel-parser 컨테이너 stop 상태에서도 /parse 성공 확인. ⚠️ 임시: compose parse-svc `EXCEL_PARSER_BACKEND=openpyxl`(이미지에 node/kordoc 없어 auto→kordoc 불가 — 2e 에서 설치 후 auto 복원) |
| **2c** | document-parser OCR(pptx+이미지) in-process 흡수 | ✅ 완료 (Task10~11, `7a0f980`+`ee39a66`) — vl_api/elements_parser/image_utils/pdf_converter/prompts 이식(config→env, gotenberg 는 httpx 직접, PDF 렌더는 PyMuPDF 직접), `ocr_file_to_elements`+`ocr_elements_sync` 진입, pdf 스캔페이지 OCR 도 in-process. document-parser stop 상태에서 png/pptx /parse 200+enriched 정상. 스택검증 발견 수정 2건: ① AsyncClient 루프 재바인드(asyncio.run per-call → "Event loop is closed") ② 순수텍스트 figure→text 재분류(blockify figure→image 매핑이 markdown 을 버려 enriched 빈 문자열 — 구 HTTP 경로에도 있던 잠재 결함). compose parse-svc 에 MODEL_API_URL/KEY·GOTENBERG_URL·GUIDED_JSON_MODE=response_format 추가 |
| **2d** | markitdown 완전 제거 + docx/폴백=kordoc + facade 파싱코드 삭제(/ingest/submit·status 제거) | ✅ 완료 (Task12~14, `13c8dc0`+`a8f9818`+`f3e73f3`) — ① `tools/kordoc.py`(CLI 래퍼)+`parsers/docx`(kordoc 네이티브, 병합표 `<table>` 보존), router docx=kordoc·fallback=kordoc, markitdown 폴백 제거. ② `parse_service/parsing.py` 삭제, `_safe_basename`→`tools.safe_basename`, blockify `PARSER_ROUTING`/`recommended_parser` 삭제(W6 측정은 역사 기록 유지), requirements markitdown 제거, 재유입 가드 `test_no_markitdown`. ③ facade `service/parsing.py`·`excel_parser_client.py`·`ingest.py` 삭제, `/ingest/submit`·`/ingest/status` 제거(`/ingest` orchestration 유지), Dockerfile.facade JRE 제거. kb-backend `/ingest/submit\|status` 참조 0건 확인 후 진행 |
| **2e** | compose 정리(excel-parser·document-parser·redis 제거) + Dockerfile 런타임 + E2E | ✅ 완료 (Task15~17, `fddbd2a`+`ee840dd`+문서커밋) — ① Dockerfile.parse-svc 에 node/kordoc(`npm i -g kordoc`) 런타임(이미지 검증 kordoc 3.8.3/java21/fitz). ② compose 에서 excel-parser·document-parser·redis 서비스+redis_data 볼륨 삭제, parse-svc `EXCEL_PARSER_BACKEND` override 제거(이미지 기본 auto)+`KBP_VL_MAX_CONCURRENT`, depends_on=gotenberg+minio, facade/adaptive_chunk 의 OCR/EXCEL URL 제거, `.env.example`·override.yml 정리. 스택 down/build/up 전 서비스 healthy(excel-parser/document-parser/redis 부재). ③ E2E: 확장자별 단독 indexed 검증(xlsx=excel_rag_parser·webp=recursive_1100·pdf=9청크·docx=kordoc `<table>` 보존). **다형식 동시적재+검색 완주는 OpenRouter LLM 처리량 지연으로 미완**(범위 밖 — 크레딧 여유 시 재확인). |

**Phase 2 파서 일원화 종료(2026-07-03)**: 파싱 fleet 이 parse-svc 단일 이미지 in-process 로 통합(java+node/kordoc+PyMuPDF). 외부 excel-parser/document-parser/redis 제거, markitdown 완전 제거, gotenberg(office→PDF)만 잔존. 상세는 02-changes §7.

발견 리스크: `test_minio_client.py` 1건 기존 red(bucket auto-create 제거 vs 테스트 미갱신 — 2a 무관, 별도 정리), 모달 테스트 4건은 `KBP_MODAL_ENRICH=1` 필요(기본 off 정책). E2E 다형식 동시적재는 LLM 처리량 의존(코드 무관).

## PDF MinerU 레인 진행 (2026-07-13)

> spec/plan `docs/superpowers/{specs,plans}/2026-07-13-mineru-pdf-integration*.md`(plan v3 READY, ultracode 4렌즈 blocking 0). 상세 변경은 02-changes §8.

| Task | 내용 | 상태 |
|---|---|---|
| 1 | triage.py(PyMuPDF 저비용 신호) 이식 = 게이트 신호원 | ✅ 완료 — test_triage 10 passed |
| 2 | `gate.py` 문서수준 라우팅(스캔 있으면 ocr 강제, 예외→ODL) | ✅ 완료 — test_pdf_gate 11 passed |
| 3+4 | `mineru_lane.py` content_list→blocks→pages 매핑 + in-process do_parse 호출(hybrid-http-client, 디스크 read, mineru 지연 import) | ✅ 완료 — test_mineru_lane 4 passed |
| 5 | `__init__.py` parse 문서수준 분기 + `_safe_decide_route` 가드 + MinerU 실패·빈결과 ODL 폴백(기존 본문→`_odl_lane`) | ✅ 완료 — test_parser_pdf_routing 5 + 기존 pdf 5 passed |
| 6 | env(`scripts/parse-svc.env` MINERU_VLM_SERVER_URL) + 배포 노트 `docs/mineru-deploy-notes.md` | ✅ 완료 |
| 7 | 전체 회귀 + _workspace 반영 | ✅ 완료 — 신규/관련 35 passed, 회귀 0(기존 red 5건 baseline 동일 확인) |
| 8 | **배포서버 스택검증(실 MinerU)** — do_parse 시그니처/출력경로/content_list enum(`text_level`) 소스 대조 + 실 스캔·혼합 PDF end-to-end | ⏳ **잔여** — 로컬(Intel Mac) MinerU 미설치, 배포서버 필요 |

**MinerU 레인 종료(로컬 구현분, 2026-07-13)**: 게이트/매핑/폴백 in-process 구현+단위검증 완료. 실 MinerU 경로는 배포서버 몫(Task 8). 리스크: (a) MinerU/torch/PaddleOCR 배포서버 설치 미검증, (b) content_list heading 이 `type=='text'`+`text_level` 로 올 수 있어 `_TYPE_TO_CATEGORY` 정정 필요(Task 8), (c) 숨은 OCR 텍스트레이어(char>20) 스캔 PDF 는 triage 가 TEXT_ONLY/LLM_NEEDED 로 오분류→'auto' 가능(§9 모니터링, image 블록은 VLM 추출).
