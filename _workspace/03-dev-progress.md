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
| **W5** | Search 머지 | ✅ 라이브러리 + ✅ 명시 mode 배선 / ✗ 자동 라우터 | `kb_pipeline/search.py`. facade `/search` 가 `mode=local\|global` 을 받고 global 은 `global_search` 직결(동시성 DB 슬롯 + 전용 LLM 타임아웃). kb 챗·프론트 토글까지 배선(B). `route()` 자동 라우팅은 **의도적 미배선** — 명시 토글로 대체 |
| **W6** | 파서 라우팅 | ◐ 권고 반영 | markitdown 병합표 손실 → 병합 중요 pptx/DOCX 구조파서 라우팅(02-changes §4). 2026-08-11 markup-lane 에서 html 이 형변환 API 밖으로 나오고 csv 가 엑셀 레인으로 이동(markitdown 재검토 후 재차 기각) |

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
- ~~**Search 라우터 배선**~~ → **해소(B, 2026-08-09)**: facade `/search` 가 `mode` 를 받고 global 은 `global_search` 직결. 프론트에 "전체 요약 검색" 토글(kb_pipeline provider 한정). `route()` 자동 라우팅은 의도적 미배선(오판 비용 최대 6분 LLM → 사용자 명시 선택).
  - **게이트 실행됨(2026-08-10)**: `scripts/check_global_rank.py` — 현업 verbatim 질문 20종 + 사규 코퍼스 리포트 10건. 축1(넓은 질문) 통과, 축2(특정 주제) 6/20 약함. **수용하고 배포 결정** — 완화는 UI 토글 안내, 근본 수정은 D38(관측 트리거 대기). 축2 는 회귀 감시 숫자로만 남기고 배포를 막지 않는다.
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
| **2c** | document-parser OCR(pptx+이미지) in-process 흡수 | ✅ 완료 (Task10~11, `7a0f980`+`ee39a66`) — vl_api/elements_parser/image_utils/pdf_converter/prompts 이식(config→env 는 httpx 직접, PDF 렌더는 PyMuPDF 직접), `ocr_file_to_elements`+`ocr_elements_sync` 진입, pdf 스캔페이지 OCR 도 in-process. document-parser stop 상태에서 png/pptx /parse 200+enriched 정상. 스택검증 발견 수정 2건: ① AsyncClient 루프 재바인드(asyncio.run per-call → "Event loop is closed") ② 순수텍스트 figure→text 재분류(blockify figure→image 매핑이 markdown 을 버려 enriched 빈 문자열 — 구 HTTP 경로에도 있던 잠재 결함). compose parse-svc 에 MODEL_API_URL/KEY·GOTENBERG_URL·GUIDED_JSON_MODE=response_format 추가 |
| **2d** | markitdown 완전 제거 + docx/폴백=kordoc + facade 파싱코드 삭제(/ingest/submit·status 제거) | ✅ 완료 (Task12~14, `13c8dc0`+`a8f9818`+`f3e73f3`) — ① `tools/kordoc.py`(CLI 래퍼)+`parsers/docx`(kordoc 네이티브, 병합표 `<table>` 보존), router docx=kordoc·fallback=kordoc, markitdown 폴백 제거. ② `parse_service/parsing.py` 삭제, `_safe_basename`→`tools.safe_basename`, blockify `PARSER_ROUTING`/`recommended_parser` 삭제(W6 측정은 역사 기록 유지), requirements markitdown 제거, 재유입 가드 `test_no_markitdown`. ③ facade `service/parsing.py`·`excel_parser_client.py`·`ingest.py` 삭제, `/ingest/submit`·`/ingest/status` 제거(`/ingest` orchestration 유지), Dockerfile.facade JRE 제거. kb-backend `/ingest/submit\|status` 참조 0건 확인 후 진행 |
| **2e** | compose 정리(excel-parser·document-parser·redis 제거) + Dockerfile 런타임 + E2E | ✅ 완료 (Task15~17, `fddbd2a`+`ee840dd`+문서커밋) — ① Dockerfile.parse-svc 에 node/kordoc(`npm i -g kordoc`) 런타임(이미지 검증 kordoc 3.8.3/java21/fitz). ② compose 에서 excel-parser·document-parser·redis 서비스+redis_data 볼륨 삭제, parse-svc `EXCEL_PARSER_BACKEND` override 제거(이미지 기본 auto)+`KBP_VL_MAX_CONCURRENT`, depends_on=gotenberg+minio, facade/adaptive_chunk 의 OCR/EXCEL URL 제거, `.env.example`·override.yml 정리. 스택 down/build/up 전 서비스 healthy(excel-parser/document-parser/redis 부재). ③ E2E: 확장자별 단독 indexed 검증(xlsx=excel_rag_parser·webp=recursive_1100·pdf=9청크·docx=kordoc `<table>` 보존). **다형식 동시적재+검색 완주는 OpenRouter LLM 처리량 지연으로 미완**(범위 밖 — 크레딧 여유 시 재확인). |

| **markup-lane** | html→`parsers/html`(형변환 미경유), csv→엑셀 레인(openpyxl 고정), xml→text 편입 | ✅ 완료 (브랜치 `feat/markup-lane`) — plan v7 READY(ultracode 6라운드). 신규 의존성 `markdownify` 1개, env 변경 없음. `verify-bundle.sh` html 왕복 스모크 추가 후 **실제 이미지로 실행 확인**(`✓ html 왕복 성공 — html_blocks=2`). 회귀 700 passed/3 skipped. 남은 한계는 `deferred.md` D39~D50 |

**Phase 2 파서 일원화 종료(2026-07-03)**: 파싱 fleet 이 parse-svc 단일 이미지 in-process 로 통합(java+node/kordoc+PyMuPDF). 외부 excel-parser/document-parser/redis 제거, markitdown 완전 제거(office→PDF)만 잔존. 상세는 02-changes §7.

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
| 8 | **MinerU 실계약 검증(Docker py3.12 컨테이너 + 라이브 VLM e2e)** — do_parse 시그니처/출력경로/content_list 스키마 소스대조 + 실 스캔 PDF end-to-end | ✅ **실증 완료(2026-07-13)** — 아래 |

**Task 8 실증 완료(2026-07-13, Docker py3.12-slim 컨테이너 + 라이브 VLM `api-mineru.ys-helperai.com`)**:
mineru 3.4.4 실설치+import+실 do_parse 로 검증. **실환경 결함 4건 발견·수정**:
1. `do_parse` **`p_lang_list` 필수 위치인자** 누락 → 크래시. `[MINERU_LANG or "korean"]` 전달(라이브 REQUIRED 재확인).
2. content_list 실스키마: heading=`text+text_level`(별도 title 타입 없음)·**chart=별도 타입**·list=`list_items`·code=`code_body` → `_content_list_to_elements` 재매핑(유실 0).
3. opencv(cv2) `libxcb.so.1`/libGL 시스템 라이브러리 누락 → Dockerfile apt 추가(`libgl1 libglib2.0-0 libxcb1 libsm6 libxext6 libxrender1`).
4. `six` 미포함(mineru pytorchocr import) → Dockerfile 추가(누락 시 HybridDependencyError 로 가려짐).
확정: **`server_url`=/v1 없는 base**(mineru 가 `/v1/chat/completions` append), model 명 `/v1/models` 자동조회(kwargs 불요), 동시성 `max_concurrency`(기본100). 설치=CPU torch+`mineru[pipeline]`(core 불필요).
**e2e 실증**: 스캔(이미지전용)PDF → hybrid-http-client(predictor init 1.19s) → 라이브 MinerU2.5 → content_list `{text, table(<table>HTML), header}`.
**표가 `<table>` HTML 로 비어있지 않게 추출**(2026-07-07 빈표 버그 MinerU 해결 실증). 내 매핑이 실출력 정확변환(회귀 앵커 `test_real_live_vlm_content_list_maps_correctly`). 로컬 단위검증 39 passed(mineru_lane 8·gate 11·routing 5·pdf 5·triage 10).

**스택 스모크 통과(2026-07-13)**: `Dockerfile.parse-svc` 에 MinerU[pipeline] 반영 → **실 이미지 빌드(6.14GB) + 컨테이너 기동(gunicorn -w4) + 스캔 PDF `POST /parse`** → enriched_content 에 `<table>` HTML + 셀값(n_blocks=3). 전 구간 실서비스 동작.
- **event-loop 결함 발견·수정(치명)**: mineru_vl_utils `http_client.batch_predict` 가 `get_running_loop()` 성공 시 `loop.run_until_complete` → FastAPI async 핸들러 실행중 루프 위 동기 do_parse 호출이 `RuntimeError: event loop is already running` → MinerU 폴백 → 빈 결과였음. `_invoke_mineru` 가 do_parse 를 **ThreadPoolExecutor 워커스레드**(실행중 루프 없음)에서 실행 → 해소(회귀테스트 추가, 로컬 40 passed).
- **multiprocessing 확인**: MinerU 는 PDF 렌더에 spawn multiprocessing(max_workers=3) 사용 — gunicorn -w4 밑에서 크래시 없이 정상(do_parse 를 import 아닌 핸들러에서 부르므로).
- **Dockerfile 캐싱 재정렬**: requirements/torch/mineru/opencv libs 를 앱 코드 COPY 앞으로 → 코드 변경 시 무거운 재설치 회피.

**잔여(실 배포)**: (a) 혼합 PDF('ocr' 강제) 실문서 1건 + 실 문서(한글 스캔) 품질 확인. (b) PaddleOCR layout 모델 빌드시 사전다운로드(첫 요청 ~215MB+ 런타임 다운로드 지연 억제). (c) 실 compose 스택(facade→parse-svc→edgequake) 배선 후 전 구간 적재검증. (d) VLM 엔드포인트 무인증 → 네트워크 접근제한 권고.

## 게이트 3-레인화 + 실 스캔문서 검증 (2026-07-14)

문서수준 게이트를 ODL/MinerU-pipeline/MinerU-hybrid **3-레인**으로 확장. **실측 근거**(신탁/약관 페이지별 triage 신호 덤프): 디지털 페이지는 char_count/image_coverage 로 텍스트/차트 구분 가능, **스캔 페이지(char=0)는 통짜 래스터라 싼 신호로 텍스트/순서도 구분 불가**(픽셀=layout 봐야 함). 그래서:
- **스캔 존재(OCR_NEEDED)** → MinerU **pipeline**(로컬 PaddleOCR-v5+layout+표, ocr). server_url 불요. 순서도/차트는 image 블록→하류 modal-enrich VL(관찰 대상).
- **스캔 없음 + 차트페이지 비율≥0.5**(`KBP_GATE_HYBRID_RATIO`) → MinerU **hybrid**(원격 VL).
- 그 외(디지털 텍스트) → **ODL**. (큰 디지털문서가 그림 몇 장에 통째 hybrid 되는 회귀는 ratio 가드로 방지 — 292p 약관=LLM 22/285=0.08→ODL.)

**backend 는 do_parse 호출당 1개**라 문서수준 단일선택이 유일 granularity(페이지별 혼합 불가 = "1개만 돌림"). 트레이드오프: 디지털多+스캔소수 문서도 전체 pipeline(디지털 재OCR).

**실 스캔문서 컨테이너 검증**(신탁 아웃라인 3p, `POST /parse`): 게이트→pipeline 자동 라우팅 확인. 결과 **n_blocks=27, 표 `<table>`(rowspan/colspan 보존), 한글 텍스트 정상**(korean PP-OCRv5 rec). 표 매트릭스·한글 온전.
- **pipeline 모델셋**: hybrid(det/rec/layout 3개)보다 +7개(표인식·수식) 필요 → 첫 요청 런타임 다운로드(384s, 1회) 후 캐시. 이후 다운로드 0(오프라인).
- **속도(dev Mac CPU)**: 캐시 후 순수 파싱 3p ≈ 2.5분(페이지당 ~50s, CPU 2378%=24코어 풀). PaddleOCR det+rec+layout+표 전부 CPU라 대용량 스캔은 느림 → 배포서버 코어수·MINERU_PDF_RENDER_THREADS 로 완화. 근본은 GPU OCR 또는 페이지 축소.
- **완전 오프라인**: `mineru.json`(models-dir 고정)+`MODELSCOPE_OFFLINE=1`+`HF_HUB_OFFLINE=1`. 단 캐시에 없는 모델은 첫 1회 받음(pipeline 모델셋 사전다운로드 권장).
- 로컬 단위검증 44 tests(gate 3레인 파라미터 포함).

**dev 실행 토폴로지(테스트용)**: 호스트 parse-svc(:19001) 대신 MinerU Docker 컨테이너를 :19001 에 기동(재현 스크립트 scratchpad `run-parse-svc-mineru.sh`). facade(:19000)는 호스트 유지. ⚠️ 컨테이너 쓰는 동안 호스트 `run-parse-svc.sh` 금지(:19001 충돌).

## 다이어그램(순서도) 검출 + ODL VL 보충 (2026-07-14)

**배경**: ODL 은 벡터 순서도의 라벨 텍스트만 뽑고 시각 구조(분기/연결)를 유실. 실측 — 정의서 p5(디지털+벡터 순서도)가 TEXT_ONLY 로 오분류돼 구조 소실. 스캔 페이지는 픽셀이라 싼 신호로 순서도 판별 불가(별도 케이스 — MinerU 레인이 chart→image 블록으로 처리).

**신호(triage)**: native-text 페이지에만 `get_cdrawings` 로 curve/line 카운트(char=0 병적 아웃라인 문서는 가드로 스킵 — "get_drawings 금지" 성능결정 유지, 디지털 ~13ms/p·292p 문서 게이트 ~5s). 검출: `curve≥30`(곡선 커넥터형) OR `line≥100 AND img≥5 AND curve≥10`(PPT 복합형). **단독 신호는 오검**(실측: line 단독=테두리 표 약관 p275 line=1249·curve=8, img 단독=아이콘/QR 약관 p12 img=11) → 복합 조건으로 약관 292p 오검 23→0.

**배선**: `RouteDecision.diagram_pages` → ODL 레인이 해당 페이지만 렌더→in-process VL(qwen) elements 서술 블록 **추가**(native 텍스트 유지, 실패 비치명). 문서수준 backend 택일과 무관한 페이지 단위 보충.

**실문서 검증**: 정의서 15p → ODL + diagram=(5,) 정확 / 약관 292p → diagram=() / 신탁·소유권 라우팅 불변. 54 tests. 실측 픽스처 4문서(정의서 p5·소유권pptx p3/p4·약관 p12/p275)를 합성 PDF 회귀로 고정.

## 스캔 레인 → PaddleOCR-VL 게이트웨이 교체 (2026-07-15)

GPU 제약(AISP 5GB — MinerU VL+PaddleOCR GPU 동시 탑재 불가)과 CPU pipeline 속도(표 236셀 3p=181s) 문제로,
스캔 레인을 **PaddleOCR-VL 게이트웨이**(api-doc.ys-helperai.com/ocr/paddleocr_vl — layout+VL+표조립 전부 GPU 서버)로 교체.
- **경위**: VL 단독(raw 프롬프트)은 표 평문화(불변식 위반, 프롬프트 탐침으로 실증) → 공식 클라이언트는 로컬 layout 재조립(반대) → **게이트웨이 = 로컬 의존 0**(httpx만) + markdown+HTML표(표준 중간표현 그대로 → hybrid_to_blocks 재사용).
- **paddle_gw 레인**(`parsers/pdf/paddle_gw.py`): 페이지 렌더 → 페이지별 병렬 POST(KBP_VL_MAX_CONCURRENT) → hybrid_to_blocks(page_idx). 페이지 계약 보존.
- **실측**: 신탁 3p 스캔 — 게이트웨이 48s vs MinerU pipeline(CPU) 181s vs hybrid 166s. 표 8개 `<table>`·한국어 정확.
- **폴백**(사용자 결정): 게이트웨이 실패/빈결과 → ODL/in-process VL(**MinerU 폴백 제외**). hybrid 레인은 디지털 차트문서용 유지, pipeline 레인 코드 잔존(미사용).
- **폴백 실전 검증**: 게이트웨이 vlm worker 장애(대량 채점 세션과 GPU 충돌 추정) 중 e2e → 자동 폴백으로 정상 결과(표 7개, 308s). 가용성 보장 확인.
- 61 tests. **잔여**: 게이트웨이 회복 후 :19001 경유 e2e 재확인(레인 자체 통과 확인용). 게이트웨이 무인증 접근제한 권고. 안정화 후 torch/mineru 이미지 제거 검토(6GB→경량).

## Plan A — 스캔 레인 layout 기반 그림·차트 처리 (2026-08-02~03)

**상태: 구현 완료, 단위검증 통과(168 passed), 라이브 §V3·§V4 통과.**
설계 문서 `~/.claude/plans/mighty-whistling-quiche.md`(v10), 인수인계 `_workspace/04-planA-handoff.md`,
실측물 `_workspace/planA-measurements/`.

게이트웨이가 노출한 `layout[].blocks[]`(block_label/block_bbox/block_content)로 스캔 페이지의
그림·차트 영역을 판별해, 해당 페이지를 통째로 VL 에 1회 보내 서술로 교체한다. 표는 paddle 정본을
승계한다. 대상은 `RouteDecision.ocr_pages`(신규 필드)로 실제 스캔 페이지에 한정한다.

라이브 검증 결과:
| 대상 | 계측 | 결과 |
|---|---|---|
| 법원통지서(글자많은 스캔+QR) | layout=1 visual=0 vl=0 | paddle 유지. 사건번호·일시 정확 |
| AI페르소나 p1-3(스캔 슬라이드) | layout=3 visual=0 vl=0 | paddle 유지(316/721/990자) |
| 부동산교재 p7·p49(그림 지배) | layout=2 visual=2 vl=2 | hybrid 발동. 6자→10자, 3자→7자 |
| ABL p17(혼합형) | tbl_backfill=1 vl_extra_tables=1 | 다이어그램 2개 다 복원 + 표 보존 |
| LICO p3(간트) | — | 997자(chart recognition 켰을 때의 46,610자 아님) |

**남은 검증**: §V5 절단 감시(누적 관측), §V6 대형 스캔 문서 소요시간 대 1800s, §V7 폴백.

### ⚠ 별건 이슈 — `degen_filter` 가 정상 표를 삭제한다 (Plan A 무관, 선행 존재)

`parse_service/parsers/degen_filter.py:is_degenerate_table` 이 **반복값이 많은 정상 표**를 퇴화표로
오판해 통째로 제거한다. 실관측(2026-08-03, LICO 주간보고 p10 "6. 요구사항" 진행현황 표):

```
셀 51개, rowspan 2, colspan 1 — paddle 이 병합구조까지 온전히 추출
상위 셀값: '0건'×18, '1건'×6, '3건'×4, '2건'×4
is_degenerate_table() → True   → 페이지 blocks 가 0개가 됨
```

R3 규칙(`dom >= 0.35 and comp < 0.36`)에 걸린다. 진행현황·체크리스트처럼 **같은 값이 반복되는 표는
정상 업무문서에서 흔하다** — 현재 규칙은 그것을 환각 반복과 구분하지 못한다.

Plan A 와 경로가 겹치지 않는다(그 페이지는 hybrid 미발동, 순수 paddle 경로). Plan A 범위 대장에서
`degen_filter` 는 명시적 비범위라 손대지 않았다. **별건으로 처리 필요.**
검토 방향(미확정): 셀값 반복만이 아니라 **행 구조의 규칙성**(열 수 일관성·헤더 존재)을 함께 보거나,
`rowspan`/`colspan` 이 있는 표는 퇴화 판정에서 제외.

### Plan B 증분 진행 (2026-08-04)

Plan B(페이지수준 라우팅)는 설계 문서를 4라운드 검증했으나 blocking 이 5→4→4→4 로 줄지 않았고,
후반에는 결함의 주된 출처가 **개정 과정에서 새로 생긴 자기모순**이었다(같은 절에서 같은 상황을
정반대로 규정, 조건 블록 안에서만 정의한 변수를 밖에서 사용 등). 원인은 `_parse_routed` 가 8가지
관심사가 얽힌 함수인데 그것을 산문 의사코드로 통째 명세하려 한 것이다.
→ **증분 5단계로 분할**(사용자 확정). 각 증분은 코드+테스트로 계약을 고정한 뒤 다음으로 넘어간다.

| 증분 | 내용 | 상태 |
|---|---|---|
| B-0 | `parsers/ocr` 동시성 정리(loop-aware 세마포어, gather, 배치 진입점) | ✅ |
| B-1 | `RouteDecision` 에 `page_lanes`·`narrate_pages`·`total_pages` 순수 추가 | ✅ |
| B-2 | `run_paddle_gateway(page_numbers=)` — 스캔 페이지만 전송 | ✅ |
| B-3 | 배치 VL seam 배선 — hybrid·서술 양쪽 | ✅ |
| B-4 | `DIAGRAM_*` 프롬프트 자기판단형 개정 + append 표 처리 | ✅ |
| B-5 | `_parse_routed` 재작성 + `vl` 레인 삭제 | 대기 |

**B-4 프롬프트 개정 실측**(2026-08-04): 순서도→흐름 서술 236자(주체·분기 정확), 표→74자 요약,
차트→213자(수치 정확), 간지→빈 배열. 1차 실측에서 "빈 배열을 반환합니다" 메타 문장이 블록으로
들어가는 것을 발견해 금지 규칙 추가.

**B-1 구현 중 발견**: 새 필드를 채우면서 `vl` 레인도 `diagram_pages` 를 갖게 되는데, 그 레인이 빈
결과로 ODL 폴백할 때 그 값이 소비돼 **오늘 없던 VL 서술이 새로 붙는다**(INFOCZ 31p, ABL 20p).
`vl` 레인만 비워 현행 동작을 보존했다(B-5 에서 레인 자체가 사라지면 예외도 제거).

**B-5 완료 (2026-08-04)** — `_parse_routed` 페이지수준 재작성, `_vl_lane`·`_VL_RATIO` 삭제.
전체 202 passed. 이제 KIS 류(그림 비율 높은 표 문서)가 문서 전체 VL 로 가지 않고 페이지마다
odl 로 가서 표가 ODL `<table>` 로 보존된다.

구현 단계에서 잡은 결함 3건(설계 문서 4라운드 검증에서는 못 잡았다):
1. thin 판정을 `page_lanes` 기준으로만 계산 → 게이트 열기 실패 문서에서 스캔 페이지가 VL 전사 누락
2. ODL 실패 시 `total_pages=0` 이라 병합 루프가 0회 → 페이지 0개 문서
3. **ODL 폴백 미작동** — `except ToolError` 인데 실제로는 `subprocess.CalledProcessError` 가 온다
   (`_odl_convert` 가 예외를 감싸지 않음). **자바 없는 환경에서 전 문서 파싱 실패**. 실측으로 발견.

### 미검증 문서 10종 실측 회귀 (2026-08-04)

단위테스트가 통과한 뒤 **한 번도 테스트하지 않은 PDF 10종**으로 돌려 숨은 결함을 찾았다.
자바가 없는 개발 PC라 전 페이지가 VL 폴백을 타는 최악 조건이었고, 그 덕에 폴백 경로 결함이 드러났다.

발견 3건:
1. **ODL 폴백 미작동**(위 3번) — 이 회귀에서 처음 드러났다. 10개 문서 전부 실패.
2. **전사 경로에 `max_tokens` 미전달** — hybrid 경로에는 상한과 실패 판정이 있었는데 전사 경로에는
   둘 다 없었다. arXiv 논문 p6(2526자)이 기본 2000 토큰에서 절단돼 빈 페이지가 됐다.
   `KBP_VL_PAGE_MAX_TOKENS` 전달 + `_looks_like_failed_vl` 적용으로 1438자 복구.
3. **모델측 퇴화 — 상한을 올려도 남는 실패**. 원인은 절단이 아니다:
   ```
   arXiv p5(목차)  finish_reason=stop  completion_tokens=226 (상한 8000)
   ```
   목차의 leader dot(`. . . .`) 반복 패턴에서 모델이 루프에 빠졌다가 **스스로 종료**한다.

   **재시도는 무효였다 — 실측 회복률 0%(5/5 실패).** 처음에 "실행마다 성공/실패가 갈리니
   재시도로 회복된다"고 보고 재시도를 넣었으나, `temperature=0.1`(vl_api.py:196)이라 **같은
   이미지는 같은 실패를 반복**한다. 회귀 실측이 이 가설을 반증했다.

   채택한 해법은 **네이티브 텍스트 폴백**이다. 이 경로에 오는 페이지는 정의상 네이티브 텍스트를
   가진 odl 레인이고(p5 4002자·p6 2527자), 렌더 시 이미 뽑아둔 `RenderedPage.text`
   (pdf_pages.py:59)를 쓰므로 추가 비용이 0이다. 그마저 없으면 빈 결과로 둔다(잘린 raw JSON 이
   본문이 되는 것보다 낫다).
4. **`degen_filter` 가 폴백을 다시 지웠다** — 폴백은 4001자로 정상 발동했는데도 최종 blocks 가
   비었다. 목차의 leader dot(`. . . .`)이 5-gram 지배 규칙(degen_filter.py:60-63)에 반복 구절로
   걸려 **페이지가 통째로 삭제**된다(압축률 규칙은 0.226 > 임계 0.16 으로 통과 — ②만 발동).
   `degen_filter` 임계는 건드리지 않고(별건 + 진짜 퇴화가 새나갈 위험) **폴백 경로에서만 점선을
   접었다**(`_strip_leader_dots`). 점선은 의미 없는 조판 장식이라 손실이 없다:
   `p5 degen True→False, 4002→1787자`(제목·페이지번호 전부 보존).
   ※ 이는 사용자가 별건으로 남긴 degen_filter 오탐(LICO p10 51셀 표)과 **같은 계열의 두 번째 사례**다.

**최종 회귀(2026-08-04)**: 10문서 전부 파싱 성공, **빈 페이지 0건**, `page_idx`·페이지수 불일치 0건.

**진단이 네 번 바뀐 기록**(각 단계를 실측이 반증했다 — 단위테스트 209개로는 한 층도 못 잡았다):
`max_tokens 절단`(p6만 맞음) → `모델 비결정 → 재시도`(회복률 0%로 반증) → `네이티브 폴백`
(발동했으나 삭제됨) → `leader dot 정규화`(해결).

**남은 관측**: 검사 스크립트의 pipe 표 경고가 실행마다 0~3건 오간다. 확인한 1건은 아키텍처
다이어그램 ASCII 아트 오탐이었고 재현 시도에서는 나오지 않았다. VL 출력 비결정성 때문으로 보이며
**별건으로 남긴다**.

**성능 관측**: 102페이지 문서가 605초. facade `KBP_PARSE_SVC_TIMEOUT` 1800s 대비 여유 3배뿐이다.
자바가 있으면 ODL 이 대부분을 처리해 훨씬 빠르지만, **대형 문서 소요시간은 별건으로 봐야 한다**
(Plan A §V6 과 같은 항목).

**페이지 정합은 깨끗**: 전 문서 `page_idx` 불일치 0건, 페이지수 불일치 0건.

---

## facade 잡 큐 — 동시처리·유량제어를 kbp 로 이관 (2026-08-03~04, 진행 중)

**배경**: kb-backend 가 없어지고 facade 가 유일한 API 서버로 남는다. 지금 facade 는 유량제어
수단이 **하나도 없다** — 4개 쓰기 경로가 전부 동기 블로킹이고(`service/app.py:112,134,221,276`)
컨테이너는 `gunicorn -w 2` 라 무거운 요청 2건이면 facade 전체가 멎는다. 반면 kb-backend 는
durable 큐를 이미 갖고 있다(`batch_ingestion_items` + `FOR UPDATE SKIP LOCKED` + lease
heartbeat + worker 레지스트리). **그 능력을 kbp 로 옮긴다.**

- 설계: [`docs/superpowers/specs/2026-08-03-facade-job-queue-design.md`](../docs/superpowers/specs/2026-08-03-facade-job-queue-design.md) (v6)
- 범위 밖(D1~D12): [`...-deferred.md`](../docs/superpowers/specs/2026-08-03-facade-job-queue-deferred.md)

### 핵심 결정

| 항목 | 결정 | 근거 |
|---|---|---|
| 기존 4경로 | **202 로 바꾸지 않는다.** 계약 유지, 내부만 잡 경유 | kb 클라이언트가 `raise_for_status()` 후 `body.get("enriched_content") or ""` 라(`kb_pipeline_client.py:175-186`) 202 를 **예외 없이 삼켜 빈 문서를 적재**한다 |
| 슬롯 위치 | DB(`kbp` 스키마), in-memory 금지 | facade 가 이미 다중 프로세스 — 세마포어는 프로세스 수만큼 샌다 |
| 제한 단위 | kind 버킷 + 테넌트. `workspace_key IS NULL` 은 테넌트 상한 면제 | 현행 `/parse`·`/chunk` 에 workspace 개념이 없어, 한 버킷에 몰면 처리량이 **현행보다 나빠진다** |
| ingest | parse·chunk·insert **3버킷 동시 예약** | ingest 는 셋 다 호출한다 — parse 만 잡으면 `LIMIT_INSERT` 의 근거가 무너진다 |
| insert 재시도 | **하지 않는다**(`max_attempts=1`) | `insert_chunks` 가 호출마다 새 문서를 제출하고 멱등키가 없다(`edgequake.py:379`). 원인을 없애면 `edgequake.py` 를 안 건드려도 된다 |
| lease 펜싱 | `(claimed_by, attempt_count)` — `attempt_count` 가 세대 토큰 | **`claimed_by` 만으로는 안 된다**(아래) |
| 커넥션 풀 | 안 쓴다. 연산마다 connect/close | `psycopg_pool` 은 `psycopg[binary]` 에 없는 별도 배포판 → 폐쇄망 기동 실패 |
| GC·멱등키·스트리밍 | Phase 1 비범위(D1·D2·D3) | 만들지 않으면 그 결함들이 존재하지 않는다 |

### 검증에서 뒤집힌 것 — `claimed_by` 만으로는 펜싱이 안 된다

v2~v5 동안 "`worker_id` 가 프로세스마다 유일하니 `claimed_by` 술어면 충분하고, 같은 프로세스가
자기 잡을 재claim 하는 경우는 worker 가 하나일 때 발생하지 않는다"고 적어 두었다. **정반대다.**
worker 가 **하나뿐일 때** 회수된 잡을 다시 집는 주체는 필연적으로 그 하나뿐인 worker 이고,
`worker_id` 는 프로세스 수명 동안 고정이다. dev·compose·airgap 모두 facade-worker 는 1개다.

```
attempt 1 (스레드 A) 실행 중 → heartbeat 랩스로 회수 → queued
같은 worker 가 재claim → attempt 2 (스레드 B). claimed_by 동일
스레드 A 의 complete 가 술어를 통과 → attempt 1 결과로 종결
스레드 B 는 계속 진행 → edgequake 에 두 번째 문서 제출   ← 중복 적재
```

스키마 변경 없이 `attempt_count` 를 세대 토큰으로 쓴다(claim 이 `RETURNING attempt_count`,
모든 쓰기가 `AND attempt_count = $gen`).

관련해 **lease 상실 처리가 부작용 기준으로 갈린다**: 부작용 *이후*(`complete`/`requeue`)는 결과
폐기, 부작용 *직전*(`set_stage('inserting')`)은 **다운스트림을 호출하지 않고 중단**. 후자를
"로그만 남기고 계속" 하면 중복 적재 방어가 통째로 무의미해진다.

### 진행 상황

| 단계 | 파일 | 상태 |
|---|---|---|
| 0 | `pyproject.toml` (`testpaths`+`requires_pg` 마커) | ✅ |
| 1 | `service/jobs/{admission,schema,repo}.py` | ✅ 45 tests |
| 2 | `service/jobs/{blobs,runner}.py` | ✅ 27 tests |
| 3 | `service/worker.py` | ✅ 11 tests |
| 4 | `service/jobs/api.py` 라우터 + `app.py` 등록 | ✅ 20 tests |
| 5 | 레거시 4경로 잡 래퍼 + `service/tests/conftest.py` 재배선 | ✅ |
| 6 | compose·런처·문서 | ✅ |

**현재 250 passed.** 기존 엔드포인트 테스트는 **단언을 하나도 고치지 않고** 통과한다 —
conftest 가 인메모리 repo/blobs + 인라인 디스패처를 자동 주입한다. 응답 계약이 유지된다는
직접 증거다. 실 Postgres(:5433) 라운드트립으로 버킷 상한·펜싱·세대 토큰·취소 3경로·
`attempt_count` 가드·기아 회피를 확인했다.

### 실측이 문서 검증을 이긴 사례

claim SQL 에 `coalesce(workspace_key, '\x00anon')` 을 sentinel 로 썼는데, 파이썬 소스에서
`\x00` 이 **진짜 NUL 바이트**가 되어 SQL 문법 오류를 냈다. ultracode 검증 4라운드가 못 잡았고
**첫 라이브 실행이 잡았다**. 애초에 sentinel 이 불필요하다 — `PARTITION BY` 는 NULL 을 한
그룹으로 묶는다. 이후로는 문서 검증보다 실행 검증에 무게를 둔다.

### 부수 정정

`service/tests/test_facade_auth.py` 2건이 **이전부터 깨져 있었다**(`testpaths` 가 `service/tests`
를 안 봐서 안 보였다). 살아있는 edgequake 에 의존하는 테스트였고 `TestClient` 가 서버 예외를
재발생시켜 주석("may 5xx if edgequake down — fine")과 다르게 동작했다. fake 주입으로 격리했다.

### 라이브 end-to-end (2026-08-04)

facade(:19000)와 facade-worker 를 실제로 띄우고 스캔 PDF 6건을 동시 제출했다.

```
t=1..6  {'queued': 2, 'running': 4}   ← parse 버킷 상한 4 가 6회 관측 내내 유지
이후    슬롯이 비는 대로 queued → running 승격, 순차 완주
```

결과는 현행 `/parse` 스키마 그대로였다(OCR 한글 884자 + `docs_id`·`page_spans`·`pages`·
`table_blocks`·`timing_metrics`·`modal_spans`). 레거시 `/parse` 도 동일 본문을 반환했다.

부산물로 §5.1 규칙도 실증됐다 — parse-svc 가 `.md` 를 거부하면(`{status:"failed"}`)
**잡은 succeeded** 이고 본문이 그대로 보존된다. 현행 `/ingest` 가 200 + 원본을 주는
정상 경로를 깨지 않는다.

### app.py 배선에서 잡은 결함 둘 (테스트가 아니라 실행이 잡았다)

1. **`dependency_overrides` 우회** — `_job_runner` 가 `get_parse_client` 함수를 그대로
   넘겨서, FastAPI 오버라이드가 무시됐다. 단위 테스트가 fake 대신 **진짜 parse-svc·
   MinIO 를 때렸다**(MinIO `AccessDenied` 로 드러남). `app.dependency_overrides` 를
   존중하는 `_resolve()` 팩토리로 고쳤다.
2. **`importlib.reload` 가 다른 모듈을 오염** — `test_facade_auth.py` 의 reload 가 `app`
   객체를 갈아치워, 알파벳 순으로 뒤에 오는 `test_insert_endpoint`·`test_parse_endpoint`
   가 든 옛 `app` 참조에 오버라이드가 걸렸다. 게이트 키를 **요청 시점 읽기**로 바꿔
   reload 자체를 없앴다(빈 문자열도 미설정과 동일 취급 — D12 의 함정 방지).

### 6단계(배포·문서)에서 실측이 잡은 것

**런처 프로세스 패턴 함정 둘** — facade-worker 는 HTTP 포트가 없어 포트로 스코프할 수
없는데, 처음 쓴 패턴이 둘 다 틀렸다.

* `pkill -f "python -m service.worker"` → **안 맞는다.** Homebrew 파이썬의 실제 cmdline 은
  `/usr/local/Cellar/.../MacOS/Python -m service.worker` 로 **대문자 Python** 이다. 이것
  때문에 e2e 검증용으로 띄웠던 worker 가 살아남아 같은 큐를 이중 소비했다(`ps` 로 발견).
* `pgrep -f "service.worker"` → **너무 넓다.** 정규식 `.` 이 아무 문자나 매치해 VS Code 의
  `--service-worker-schemes=...` 까지 잡혔다.

정답은 `pgrep -f -- '-m service\.worker'`. 그리고 `stop_worker` 가 PID 파일**만** 보면
런처를 안 거치고 띄운 고아가 살아남으므로, PID 파일과 패턴 매치를 **둘 다** 모아 죽인다.

**pytest 가 공용 dev DB 를 오염시켰다** — `test_job_repo_pg` fixture 가 앞만 비우고 뒤를
안 비워서 `pytest:*` worker 행과 running 잡이 남았다. `GET /jobs/workers` 가 없는 worker 를
capacity 에 더해 보고했다. fixture 에 teardown 을 넣었다.

**stale 회수 라이브 확인** — heartbeat 가 10분 지난 유령 worker 행을 직접 주입했더니
(a) 집계는 즉시 제외했고(60s 창), (b) 다음 claim 틱이 행 자체를 DELETE 했다. 설계 §3.1(1c)
와 §4.2 가 주장하는 자가치유가 실제로 돈다.

**라이브 최종 확인**: `scripts/run-facade.sh` + `scripts/run-facade-worker.sh` 로 띄운 뒤
잡 제출 → `succeeded`(attempt 1). `docker compose config` 로 dev·airgap 양쪽 문법과
**airgap 이미지 9종 불변**(facade/facade-worker 가 `kbp-facade:airgap` 공유)을 확인했다.

## Phase 2 시작 — kb 클라이언트 잡 경로 (2026-08-04, 비파괴 증분)

**선행 D1(제출 멱등키) 완료** 후 착수. kb 레포에 미커밋 31파일(`pipeline.py` 422줄)이
진행 중이라 **전면 전환 대신 플래그 뒤 증분**으로 갔다.

- `kb_pipeline_use_jobs`(기본 `False`) — 켜면 `POST /jobs/{kind}` → 폴링 → `/result`.
- 건드린 파일: `kb_pipeline_client.py`·`config.py`·`dependencies.py` **셋뿐**.
  `pipeline.py`·`batch_worker.py`·프론트는 미접촉.
- 롤백: env 하나. 코드 변경 불필요.
- 응답 본문이 레거시와 동일해 **매핑 코드를 손대지 않았다** — 세 호출 지점의
  `_request → raise_for_status → body` 를 `_post_body()` 하나로 갈아끼웠다.
- 멱등키를 kb 가 직접 준다(`kb-parse:{docs_id}`, `kb-insert:{ws}:{doc_id}`). insert 는
  edgequake 에 멱등키가 없어 재시도가 곧 중복 문서라 반드시 필요하다.
- 잡 실패는 **예외로 표면화**한다. 조용히 빈 결과를 돌려주면 빈 문서가 적재된다
  (이 프로젝트가 202 전환을 포기한 바로 그 이유).

kb 테스트: 클라이언트 31 passed(신규 6), 클라이언트를 쓰는 파이프라인 포함 81 passed.

**남은 것**: 플래그를 켠 라이브 검증 → `batch_worker` 제거 → 프론트 배치 화면을
`GET /jobs?batch_key=` 로 전환.

### Phase 2 라이브 검증 (2026-08-04, 플래그 ON)

`KB_PIPELINE_USE_JOBS=true` 로 kb-backend 를 띄우고 실제 문서를 kb API 로 업로드했다.

```
POST /kb/{id}/documents  →  200 {job_id, status:"queued"}
kbp.jobs:  parse succeeded  legacy=False  attempt=1  52.9s
           idem_key = h:kb-parse:5ac76890503439cb
```

**검증된 것**

- kb 가 레거시 `/parse` 가 아니라 **신규 `/jobs/parse` 로 갔다**(`legacy=False` 가 증거 —
  레거시 래퍼가 만드는 잡은 `legacy=True` 다).
- kb 가 **명시 멱등키를 실었다**(`h:` 접두사 = `Idempotency-Key` 헤더 경유). 자동 파생이면
  `a:` 접두사에 시간 버킷이 붙는다.
- 제출 → 폴링 → `/result` 회수가 전부 돌았고, `attempt=1` 이라 재시도 없이 한 번에 끝났다.
- **플래그 off 회귀**: 같은 클라이언트를 `use_jobs=False` 로 만들어 레거시 경로가 그대로
  동작함을 확인(885자). 롤백이 실제로 된다.

**1차 시도(임베딩 다운)**: `chunk` 잡이 `AllMethodsFailedError ... HTTP 429 No deployments
available. Passed model=bge-m3` 로 실패했다. 레거시 `/chunk` 도 **동일하게 500** 이라
내 변경과 무관한 환경 문제로 확정하고 보류했다.

**2차 시도(임베딩 복구 후) — 전체 체인 완주**

```
parse   succeeded  44.8s  attempt=1  legacy=False  idem=h:kb-parse:5ac76890503439c
chunk   succeeded  60.3s  attempt=1  legacy=False  idem=a:159ffc1672a1ba410c9beb75
insert  succeeded  43.8s  attempt=1  legacy=False  idem=h:kb-insert:1d9c9928-31c9-
```

kb 문서 상태 `ready`, 청크 1건이 실제 OCR 한글 내용으로 적재됐다. 세 단계 모두
`legacy=False`(신규 `/jobs/*` 경로) · `attempt=1`(재시도 없음)이다.

멱등키 접두사가 kind 별로 다른 것도 의도대로다 — parse·insert 는 kb 가 명시 키를 싣고
(`h:`), chunk 는 kb 가 안 실어서 facade 가 자동 파생했다(`a:` + 시간 버킷). chunk 는
선행 parse 결과가 정해지면 내용이 결정적이라 자동 키로 충분하다.

**부수 관측 — 잡 경로의 실익이 드러났다.** 레거시 `/chunk` 를 curl 로 부르면 4방법 경쟁이
180s 를 넘겨 클라이언트가 먼저 끊긴다(`code=000`). 잡 경로는 제출이 즉시 끝나고
`stage=chunking` 을 폴링으로 보다가 48~60s 뒤 결과를 회수한다 — 소비자 타임아웃과
무관해진다.

(관련: `_workspace` 의 "LLM 백엔드 현황" 메모 — OpenRouter/litellm 가용성이 오락가락한다.)

## Phase 2 후속 — deferred 정리 (2026-08-05)

facade 가 유일한 API 서버가 되는 방향에서, 잡 큐 도입 때 범위 밖으로 뺐던 항목
(`docs/superpowers/specs/2026-08-03-facade-job-queue-deferred.md`)을 우선순위대로 처리했다.
**20건 중 7건 종결 / 12건 보류 / 1건 안 함.**

### 이번에 종결한 것

| | 무엇 | 왜 위험했나 |
|---|---|---|
| **D12** | 폐쇄망 `KBP_FACADE_KEY` 필수화 | compose 가 키를 아예 안 넣어 게이트가 **항상 꺼진 채** 호스트 3000 으로 떠 있었다. 무인증 적재·검색·삭제 |
| **D20** | `parse-staging/` 누적 | 323건 214.7MB. 95% 가 미리보기만 하고 적재 안 한 이탈분 |
| **D5** | insert 재시도 중복 적재 | 회수 경로만 막혀 있고 **재시도 경로가 뚫려** 있었다 |
| **D13** | 소진된 `queued` 좀비 | 부모 TTL 삭제를 **영구 차단** — GC 를 만들어 놓고도 안 지워진다 |
| **D8** | 배포 health 검증 | 호스트 포트가 compose 매핑과 전부 어긋나 **사실상 전량 오탐** |

### 설계상 짚어둘 것

- **D5/D13 — 방어는 모든 경로에 대칭이어야 한다.** `stage='inserting'` 가드가 회수
  경로(`_recover`)에만 있고 재시도 경로(`requeue`)에 없었다. edgequake 제출 후 폴링에서
  5xx 가 나면 `classify` 가 재시도로 분류하므로, `ingest`(max 3)가 parse 부터 다시 돌며
  같은 문서를 또 제출했다. `insert`(max 1)는 중복 대신 좀비가 됐다. **한쪽만 막으면
  나머지로 샌다** — 두 경로의 대칭을 테스트로 고정했다.
- **D20 — 수명을 아는 쪽이 지운다.** facade 는 `parse-staging/` 객체를 누가 아직 쓰는지
  모른다(참조가 kb DB 에 있다). 그래서 잡 큐 GC 의 "행이 없으면 고아" 판정을 쓸 수 없고
  나이로만 지운다. 두 갈래에 TTL 을 달리 준다 — preview 1h, batch 7d. **배치에 1h 를
  적용하면 실패 항목 재수행이 죽는다**(그 원본을 그대로 다시 쓴다).
- **D8 — 스크립트가 사실을 두 번 적지 않게.** 헬스 URL 을 스크립트가 따로 들고 있으니
  compose 와 어긋났다. `podman inspect` 로 compose 가 정의한 healthcheck 결과를 읽게
  바꿔 사실의 출처를 하나로 만들었다.
- **worker 없는 배포는 "healthy" 로 보인다.** facade-worker 는 HTTP 를 안 열어 헬스체크에
  안 잡히는데, 없으면 `/parse`·`/ingest` 접수가 503 이다. `/jobs/workers` 의 `online`
  확인을 배포 스크립트와 문서 스모크 양쪽에 넣었다.

### 곁가지로 드러난 결함

- **parse-svc 가 없는 페이지 이미지 키를 응답에 실었다.** `put_page_image()` 반환값을
  버리고 키를 무조건 채워서, MinIO 가 불통이면 소비자가 존재하지 않는 객체를
  `chunks_meta` 에 저장하고 인용 이미지가 조용히 404 가 됐다. 실패 시 `None` 으로 바꾸고,
  첫 실패 후 남은 페이지 업로드를 중단하게 했다(7페이지 68.7s → 15.7s).
- **엑셀 게이트 룰 설정이 거짓말을 하고 있었다.** `excel_gate_default_disabled_rules =
  ["3.1","3.2","3.3","6.1"]` 은 **죽은 카탈로그**(multipart `/v1/check`, 호출부 0건)의
  id 다. 현행 게이트는 `gate_summary` 기반이라 룰 코드 체계가 아예 다르고
  (`conflicting_code_mapping` 등), `ExcelCheckRequest` 는 `disabled_rules` 를 받지도
  않는다. "셀병합·취소선을 껐다" 로 읽히지만 그 룰들은 그대로 돌고 있었다. 제거했다.
- **배포 스크립트가 남의 컨테이너를 집었다.** 이름 정규식이라 `postgres` 가 kb 스택의
  `kb-postgres` 를, `minio` 가 `dify-1-7-minio-1` 을 집었다. MinIO **버킷 생성**도
  `grep -i minio` 라 남의 버킷에 만들 뻔했다. compose 라벨 기반으로 교체.

### 남은 12건 — 다음 후보

폐쇄망 배포가 임박하면 **D16**(버킷 공유 시 스윕이 서로의 staging 삭제)이 D8 과 같은
시점 검토 대상이다. 그 외에는 D6(취소 반응성) → D10(`/communities/build` 큐 편입) 순.
나머지(D3·D4·D7·D9·D11·D14·D15·D17·D18)는 관측 가능한 문제가 생길 때 다시 본다.

## 커뮤니티 야간 배치 A1 — 구현 완료 (2026-08-09)

브랜치 `feat/community-nightly-a1`. plan:
`docs/superpowers/specs/2026-08-09-community-nightly-A1-scheduler-plan.md`(v5, READY).

**완료**
- 스키마 3종(`graph_touch`·`community_builds`·`batch_runs`), repo 11개 메서드,
  `InMemoryJobRepo` 대응, 러너 배선(touch + 이력), `submit_job_ex`,
  **`service/community_schedule.py`(신규)**, `worker.py` 스레드
- 배포: compose ×2 앵커(`TZ` + `KBP_COMMUNITY_*` 7개), env 템플릿 3종,
  `parse-only-up.sh` 가 파서 전용 배포에서 야간 스레드를 **강제 비활성**
- kb: 적재 tail 트리거 제거(함수·arq 등록은 보존)

**테스트** — kbp 303(PG 없음)/385(PG 포함), kb 648. 회귀 0.
회귀 시뮬레이션 **8종**이 실제로 빨강이 되는 것을 확인했다(구현을 되돌려 실측).

**남은 것**
- **A2** 그래프 변화 스킵(`skip_if_unchanged`·`graph_counts`·`fail_streak`·
  `max_communities`) — 없으면 vector-only 가 아닌 KB 는 그래프가 안 변해도 매 밤 재빌드
- **A3** 리포트 세대 정리(`store_reports(replace=)`) + 좀비 방어 — 재빌드로 커뮤니티가
  줄면 낡은 리포트가 영구히 남는다
- **A4** 스테일 리포트 스윕 — `DELETE /doc` 으로 문서를 전량 지운 workspace
- **B** global 검색 배선(`2026-08-06-global-search-button-plan.md`) — **지금은 리포트를
  읽는 경로가 없다**. A3·A4 의 실효성은 B 가 배선된 뒤에야 체감된다
- 폐쇄망 번들 재포장(스케줄러 env 7개가 들어간 compose·템플릿 반영)

**운영 확인 필요(미실측)** — 실제 야간 발화. dev 에서 `KBP_COMMUNITY_BUILD_AT` 를
현재+1분으로 두고 한 번 돌려보는 것이 남았다(계획서 §3 완료판정).

---

## A1 + B 통합 머지 (2026-08-10)

`feat/community-nightly-a1`(A1) 과 `feat/global-search-button`(B) 를 통합 브랜치
**`feat/fileconvert-api`** 에 합쳤다. `main` 이 아니다 — main 은 149커밋 뒤이고 두
브랜치의 조상일 뿐이며, 폐쇄망 3커밋도 fileconvert 에 있다.

두 작업은 커뮤니티 리포트의 **생산자/소비자** 양쪽이라 함께 있어야 의미가 있다.

### 커뮤니티 빌드 진입점이 셋 → 하나 (D22 해소)

| 진입점 | 상태 |
|---|---|
| 적재 성공 시 자동 트리거 | A1 이 제거 |
| 검색의 `build_if_missing` | B 가 `build_if_missing=False` 로 차단 (`service/app.py`) |
| 야간 배치 + 수동 `POST /communities/build` | **유일하게 남음** |

이제 리포트 생성 시점이 예측 가능하다. A2/A3/A4 를 다루기 쉬워진 전제다.

### 충돌 7건 — 전부 "양쪽 순수 추가"

env 3종·compose 2종은 같은 위치에 서로 다른 키를 넣어 양쪽을 보존했다.
`_workspace/02-changes.md` 는 **생산자(A1) → 소비자(B)** 순으로, deferred 문서는
**번호순(D25~D37 → D38)** 으로 배치했다. `service/jobs/schema.py` 는 자동 병합됐고
락 4종(`LOCK_OBJ_GLOBAL_SEARCH=4` 포함)·테이블 6종이 모두 살았음을 확인했다.

### 검증

| 대상 | 결과 |
|---|---|
| kbp (PG 없이) | **643 passed, 3 skipped** (A1 단독 미포함 B 609 + A1 34) |
| kbp (실 PG) | **738 passed, 0 failed** — 두 스키마 합본 DDL 확인 |
| compose ×2 | `docker compose config` 전체 보간 통과(더미 env 104키). facade·facade-worker 양쪽에 `KBP_COMMUNITY_*` 9키(`KBP_COMMUNITY_TZ` 포함)+`KBP_GLOBAL_*` 2키 전달 확인, parse-svc 는 0(파서라 정상). 컨테이너 `TZ` 는 D33 해소로 제거됨 |
| kb `backend/tests/` | **647 passed, 20 failed** — A1 이전 커밋에서 같은 인터프리터로 측정한 기준선 **640/23** 대비 **새 실패 0건, 3건 회복**(`table_blocks` 하네스 수정) |

> **수치 정정**: B 작업 중 "기준선 648 passed/16 failed" 로 보고했는데 오늘 재측정에서
> 재현되지 않았다(같은 코드에서 20 failed). 그래서 A1 이전 커밋에 워크트리를 만들어
> **같은 시점·같은 인터프리터로** 다시 재어 비교했다 — 그 기준선이 23 failed 다.
> **델타(새 실패 0 · 3건 회복)는 두 측정 모두에서 동일**하고, 그게 판정 근거다.
> 절대 실패 수는 세션 간 재현되지 않으므로 완료 판정에 쓰지 않는다.
>
> 실패 20건은 전부 기존 군집(gate/job_status/pipeline/readyz/raganything/ragflow)이고
> A1·B 가 건드린 파일이 아니다. 표본 확인 하나: `test_kb_provider_accept.py` 의
> `kb_pipeline_timeout_seconds == 1800.0` 은 **낡은 테스트**다 — 코드 기본값이 `a9e7072`
> 에서 3600.0 이 됐는데 기대값을 안 고쳤다(env 누출 아님). 이 군집 정리는 별건.

### 이 머지에서 잡은 배포 차단 버그 — `verify-bundle` 의 `val` 미정의

`check_env` 가 `val KEY "$envf"` 를 쓰는데 **그 함수가 정의돼 있지 않았다.**
`command not found` 로 빈 문자열이 되어 `[ "$(val X)" = "paddle_gw" ]` 가 항상 거짓,
즉 **paddle_gw 가드가 배포된 채로 죽어 있었다.** 가드가 사라진 게 아니라 통과해버리는
쪽이라 더 위험하다. 대조 실측: 구버전 exit 0 / 발화 0건 → 정의 후 exit 1 / 정확히 발화.

같은 커밋에 A1/B env 가드를 넣었다.
- 배치 켜짐 + `TZ` 빔 → **차단**. 컨테이너 기본 UTC 라 `BUILD_AT=03:00` 이 **KST 정오**에
  열려 목적이 정확히 뒤집힌다(실패가 아니라 **잘못된 시각에 성공**이라 로그로 안 드러난다).
- `DEADLINE_MINUTES <= WINDOW_MINUTES` → **차단**(창 안 제출을 그 밤에 즉시 취소).
- `KBP_GLOBAL_SEARCH_CONCURRENCY=0` → 경고(파서 전용은 정상, 전체 스택이면 버튼이
  보이는데 항상 503).

가드 7경로를 실제로 돌려 확인했다. **D35(env 템플릿에 `KBP_COMMUNITY_*`/`TZ` 추가)는
이 머지로 해소** — `scripts/parse-svc.env.example` 은 파서 전용 템플릿이라(facade 키 전무)
제외가 맞다.

---

## 폐쇄망 현장 배포 성공 (2026-08-10)

**결과** — 실제 서버(RHEL + podman, CNI)에 기동 성공. `airgap-onsite-checklist.md` §5 의
"CNI 는 한 번도 검증되지 않았다" 가정은 이걸로 해소됐다. 여전히 미검증인 것은 온프렘
LLM/VL **실연동 품질**과 대용량 동시 적재다(목업으로만 확인).

### 반입 세트

| 번들 | 크기 | `.env` |
|---|---|---|
| `kbp-parse-bundle-amd64.tar.gz` | 2.0GB | **채워서 동봉**(600, 실 비밀값) |
| `kbp-airgap-bundle-amd64.tar.gz` | 2.9GB | 템플릿 |
| `kb-airgap-bundle-amd64.tar.gz` | 0.5GB | 템플릿 |

각각 `.sha256` 를 **쌍으로** 반입한다(빼먹으면 현장에서 `sha256sum -c` 불가).
분할하지 않는다 — 2GB 를 넘어도 단일 파일이다(2026-08-10 방침).

### 스크립트는 스택당 하나, 멱등

```
sha256sum -c → tar xzf → ① .env → ② verify-bundle --env .env [--parse-only] → ③ *-up.sh
```

`--env .env` 를 **반드시** 붙인다. 빼면 `verify-bundle.sh:263` 의 `*)` 로 떨어져 아직
로드하지 않은 이미지까지 검사하고 무조건 실패한다 — 운영자는 진짜 env 문제인지 가드
오작동인지 구분할 수 없다.

### 현장에서 밟은 배포 차단 함정 3가지

1. **"fitz 없음" 이 사실은 옛 이미지였다.** 번들은 멀쩡했다(번들 tar 를 꺼내 로드해 같은
   명령 → `OK`). 스토어의 `localhost/kb-api:airgap` 이 이전 반입분이었고 오늘 tar 는 아직
   로드된 적이 없었다. kb `load-and-up.sh` 는 `.env` 검증(:88)에서 죽고 `podman load` 는
   그보다 뒤(:116)라 도달하지 못한다. → `podman load -i images/kb-images-amd64.tar.gz` 먼저.
   **가드의 구멍(미수정)**: `check_imports` 는 스토어 이미지를 검사할 뿐 **번들 출처를
   확인하지 않는다.** `docker save` 가 config 를 재작성하므로 manifest 의 config digest 와
   로컬 image ID 단순 비교로는 판정 불가(postgres 로 교차확인) — 로드해서 돌리거나
   이미지 생성시각을 봐야 한다. `parse-only-up.sh` 는 로드(:89)가 `.env` 검사(:92)보다
   먼저라 이 함정이 없다.
2. **채워진 `.env` 를 `cp` 로 덮으라던 안내.** `build-bundle.sh` 가 `PARSE_ONLY_ENV` 로 실
   비밀값을 넣어놓고도 `cp .env.parse-only.example .env` 를 출력했다. 그대로 따르면 64키를
   현장에서 다시 입력해야 한다. `ENV_EMBEDDED` 분기로 수정(`d05e755`).
3. **비밀값 백업이 gitignore 를 빠져나갔다.** 규칙이 `bak-`(하이픈)만 막아
   `scripts/parse-svc.env.bak.134521`(점)이 추적 대상으로 떴다. 점 형태 추가.
   히스토리에 비밀값 env 가 커밋된 이력은 없음을 확인했다.

### env 단일 출처 통합

리포 루트 `.env` 를 단일 출처로 만들었다(38 → 58키). 게이트/트리아지 임계 16종 +
`MODEL_NAME` + `VL_MAX_TOKENS` + OCR 게이트웨이 URL 이 `scripts/parse-svc.env` 에만 있어서
**호스트 dev 는 정상인데 compose 로 띄우면 18개가 빈 값**이었고, `.env` 의
`KBP_OPENAI_API_KEY` 가 빈 값이라 모달 LLM 이 `KeyError` 였다. 이제 `parse-svc.env` 는
`scripts/sync-parse-svc-env.sh` 로 `.env` 에서 파생시킨다.

실측: 런처 실효값 통합 전후 변화 **0건**, `.env` 단독으로 필수 5키 해소,
dev/airgap compose config 양쪽 보간 성공.

로더(`scripts/lib/load-dev-env.sh`)는 **CLI > `.env` > 레거시** 순이고 **빈 값을 "없음"
으로 취급**한다 — 이게 없으면 템플릿에서 복사한 `.env` 의 `KEY=` 가 레거시 파일의 실 키를
덮어써 기동이 죽는다(실측).
