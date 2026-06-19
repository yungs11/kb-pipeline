<!-- plan-version: v9 -->
<!-- codex-validation: READY v9 at 2026-06-19T07:42:06Z -->
<!-- evidence-note: edgequake가 이 repo에 clone됨. 실제 Rust 워크스페이스 = `edgequake/edgequake/`(자체 Cargo.toml; crates/·migrations/ 여기). 아래 모든 edgequake 경로는 이 nested 워크스페이스(`edgequake/edgequake/`) 기준. 루트 `edgequake/crates/`는 별도 crate 세트라 무시. -->

# 지식베이스 파이프라인 — SoT v2

> v1(원본 `SoT.v1.md`)은 방향성 메모였다. v2는 4개 참고 레포(`excel-parser-markitdown`, `adaptive_chunk`,
> `raganything_svc`, edgequake 본체 — 현재 이 repo `edgequake/edgequake/`에 clone)를 코드/로그 레벨로 검증한 뒤,
> 모순을 제거하고 "차용 vs 신규"를 작업항목으로 확정한 실행 스펙이다. 근거 파일위치는 [부록 A] 참조.

---

## 0. v1 → v2 변경 요약 (해소된 모순)

| v1의 문제 | v2의 해소 |
|--|--|
| "adaptive_chunk → edgequake insert"가 정면충돌(edgequake가 내부 청킹) | edgequake **공개 `ChunkingStrategy` trait**에 adaptive_chunk를 꽂아 해소 (§3.4, W1) |
| "edgequake 스키마 차용" vs "edgequake에 insert" 혼재 | **edgequake를 엔진으로 운용**(베이스), 앞단만 커스텀으로 확정 (§1) |
| per-KB "schema/RLS" 표현 모호 | edgequake가 **실제 Postgres RLS 보유**(`009/013/022`) → 공유테이블+tenant/workspace RLS로 확정 (§4) |
| 파서 분담이 실측과 불일치 | 확장자별 파서 **실측 재확정**(§3.1), markitdown=일반문서 우세는 사용자 실측 채택 |
| raganything 교체 여부 미결 | raganything는 **엔진이 아니라 "모달 LLM 서술" 아이디어만 차용** (§3.3) |
| 그래프 이중생성 위험 | content를 **단일 스트림**으로 만들어 edgequake가 추출/그래프 단독 소유 → 이중생성 제거 (§3.3–3.5) |

---

## 1. 아키텍처 결정 (ADR)

**결정: 경로 (나′) — edgequake를 베이스 엔진으로 두고, 앞단(파싱·블록화·모달서술·청킹)만 커스텀으로 만들어 edgequake에 청크를 주입한다.**

- 핵심 근거: edgequake는 단일 Postgres(pgvector+AGE)에 **추출·임베딩·그래프·커뮤니티·검색 + 진짜 RLS 멀티테넌시**를 이미 갖춘 완결 엔진이다. 이를 다시 만들지 않는다.
- 결정적 발견: edgequake-pipeline에 **`ChunkingStrategy` 공개 trait**(`Chunker::with_strategy`, async `chunk()`)가 있어, 우리 `adaptive_chunk`를 **문서화된 확장점**으로 끼울 수 있다. 따라서 "내 청킹 + edgequake 나머지"가 포크 해킹이 아니라 정식 확장으로 성립한다.

**기각된 대안**
- **(가) markdown만 edgequake API에 POST**: edgequake가 자기 청커로 재청킹 → adaptive_chunk 사장. MVP로는 가능하나 우리 청킹 자산을 못 살림 → 폴백으로만 보존(§9).
- **(다) 순수 자체 스택**(인서터·추출·커뮤니티·정합성 전부 자작): 제어 최대지만 edgequake가 이미 한 W2~W5를 재구현. 비용 과다.
- **(B) LightRAG + Qdrant/Memgraph**: 베스트 엔진이나 **RLS 상실**(앱이 격리 책임) + 커뮤니티 리포트는 LightRAG 기본 미제공(키워드 방식). 보안 요구(금융/신탁 맥락)와 SoT의 커뮤니티 리포트 요구에 불리.

---

## 2. 전체 파이프라인

```
[1 Parse] 확장자별 최고급 파서
        ↓  표준 중간표현 = "markdown + inline HTML 표"
[2 Blockify] hybrid_to_blocks() → 블록 리스트(content_list류)   ← 신규 W0
        ↓  (VLM 경로는 서비스의 elements[]를 직접 매핑)
[3 Modal enrich] 표/이미지/수식 블록 → LLM 서술 → content에 atomic 인라인  ← 신규 W2
        ↓  단일 enriched content 스트림 (텍스트 + 서술된 모달, modal_id 마킹)
[4 Chunking] edgequake Pipeline(content) + ChunkingStrategy=AdaptiveChunkStrategy
        ↓     → adaptive_chunk(HTTP) → ChunkResult[]   ← 신규 W1(공인 trait 구현)
[5 Insert] edgequake: 엔티티/관계 추출 → 임베딩 → 그래프 → 단일 Postgres(pgvector+AGE) 적재(RLS)
        ↓     (옵션) 모달 앵커 엔티티 명시 등록
[6 Community] edgequake Louvain detect_communities + LLM 리포트 — 배치(스케줄/임계)  ← 신규 W3
[7 Search] pgvector KNN + AGE 순회 + community 리포트 머지, per-KB RLS
```

**불변식(invariant): 청크는 KB당 단일 우주.** 하나의 enriched content → 하나의 청크 집합 → 그 위에서 벡터·그래프가 같은 `chunk_order_index`/`source_id`로 묶인다. 벡터용/그래프용 청크를 절대 분리하지 않는다(정합성·인용 일관성).

---

## 3. 스테이지별 명세

### 3.1 Parse — 확장자별 파서 (실측 확정)

| 확장자 | 파서 | 출력 | `<table>` HTML | 비고(실측) |
|--|--|--|--|--|
| XLS/XLSX/HWP/HWPX/HWPML | **kordoc** | md + HTML 표 | ✅ 병합 colspan/rowspan 보존 | 모든 Excel 출력에 `<table>` 확인. HWP/HWPX/HWPML은 kordoc 신뢰(실측 생략, 사용자 결정) |
| PDF | **OpenDataLoader** | md + HTML 표(`markdown_with_html=True`) | ✅ 실측(06=70개, pipe=0) | JRE 11+ 의존 |
| pptx | **markitdown(단순)/kordoc·MinerU(병합)** | md | markitdown ❌ / 구조파서 ✅ | **W6 실측**: markitdown은 병합(colspan/rowspan) 전부 소실(파싱시점, blockify 복구불가). 일정·간트·매트릭스 등 병합 중요 → kordoc/MinerU 라우팅 |
| DOCX | **markitdown(단순)/kordoc·MinerU(병합)** | md | 동일 | **W6 실측**: 헤더 colspan 등 병합 소실 → 병합 중요시 구조파서. 텍스트형은 markitdown |
| image/scanned PDF | **VLM/OCR(:18050)** | md + HTML 표 **+ `elements[]`** | ✅ + 구조배열 | `content.{markdown,html,text}` + `elements[{category,content}]` 보유 |

- **표준 중간표현**: "markdown + inline HTML 표". kordoc·OpenDataLoader·VLM은 네이티브로 이 형태. markitdown만 pipe표 → §3.2에서 HTML로 승격.
- 파서 추상화: 각 파서를 `Parser.parse(path) -> {fmt, text/html, elements?}` 어댑터로 감싼다(기존 `compare/adapters/*` 패턴 재사용 가능). VLM은 `elements[]`를 추가로 노출.

### 3.2 Blockify — `hybrid_to_blocks()` (신규 W0)

"markdown + inline HTML 표"를 **블록 리스트**로 변환한다. `markdown-it-py(html=True).enable("table")`로 파싱:

- `html_block`이고 `<table` 포함 → `{type:"table", table_body:<HTML 그대로>, page_idx}`
- `heading_open` → `{type:"text", text, text_level:N, page_idx}`
- `table_open`(markitdown pipe표) → HTML로 렌더 → `{type:"table", table_body:<HTML>, page_idx}`
- 이미지(`<img>`/`![]()`) → `{type:"image", img_path, image_caption, page_idx}`
- 수식(`$$`/math 스팬) → `{type:"equation", latex, text_format, page_idx}`
- 그 외 inline 텍스트 → `{type:"text", text, page_idx}`

규칙: ① 블록 1개 = 구조 단위 1개(문서 순서 유지), ② **표는 HTML 보존(절대 pipe로 납작화 금지)**, ③ `page_idx` 있으면 부여(없으면 0/누적), ④ `text_level`로 섹션 계층 유지.
**VLM 경로 예외**: 서비스 `elements[]`(category=text/table/…)를 직접 블록으로 매핑(가장 충실).

블록 스키마 → [§5.1].

### 3.3 Modal enrichment — 표/이미지/수식 LLM 서술 (신규 W2, raganything 아이디어 차용)

raganything `modalprocessors`의 **구조만** 차용한다(엔진 채택 아님). 각 모달 블록을 타입별 프롬프트로 LLM에 보내 자연어 서술을 만든다:
- **table/equation**: HTML/LaTeX 텍스트 → **텍스트 LLM** 서술(비전 불요).
- **image/figure(스캔)**: `img_path` → **비전 LLM** 서술.

**합류 방식(사용자 결정 = 고급안: 독립 모달청크 + 엔티티):**
1. 서술을 **atomic 마킹 블록**으로 enriched content에 인라인한다. 예:
   `〈MODAL id="T1" type="table"〉{LLM 서술}\n{table_body HTML}〈/MODAL〉`
2. **원자성은 `AdaptiveChunkStrategy`(§3.4, Rust)가 소유**한다 — `〈MODAL〉…〈/MODAL〉` 영역을 한 덩어리로 떼어 **단독 atomic 청크**로 만들고, 그 사이의 텍스트 갭만 adaptive_chunk `/chunk`에 보낸다.
   ⚠️ **검증결과(codex)**: adaptive_chunk의 **어느 엔드포인트도** `〈MODAL〉` 스팬 원자성을 강제하지 않는다 — `/chunk`엔 원자블록 처리가 없고, `/chunk/jobs/file`의 `parsed.blocks`는 **BI 스코어링용**일 뿐 스팬 분할을 강제하지 않는다. 따라서 모달 원자성은 **전적으로 `AdaptiveChunkStrategy`가 소유**한다(W1 책임).
3. 그 청크를 edgequake가 임베딩(검색가능) + 엔티티/관계 추출 → **모달이 그래프 엔티티로 등록**(§3.5). `modal_id`를 청크 metadata에 보존.
4. (옵션) **결정적 앵커 엔티티**가 필요하면 적재 후 `POST /api/v1/graph/entities`로 "○○표(table)" 노드를 명시 등록하고 `source_id`로 그 청크에 연결.

→ 이로써 표/그림이 **검색가능 텍스트 + 그래프 노드**로 승격되며, **그래프는 edgequake 추출이 단독 생성**(이중생성 없음).

### 3.4 Chunking — adaptive_chunk를 edgequake `ChunkingStrategy`로 (신규 W1)

edgequake는 `Chunker::with_strategy(config, Arc<dyn ChunkingStrategy>)`로 커스텀 전략을 받는다. `ChunkingStrategy::chunk(text, config)`는 **async**다.

- **`AdaptiveChunkStrategy`(Rust)** 를 구현. `async chunk(content, config) -> Vec<ChunkResult>`의 책임:
  1. content를 `〈MODAL〉…〈/MODAL〉` 경계로 분리.
  2. **모달 영역** → 각각 **단독 atomic `ChunkResult`** 로 즉시 방출(분할 금지).
  3. **텍스트 갭** → `adaptive_chunk` 서비스(`POST /chunk`) HTTP 호출 → 응답 `chunks[]` 매핑.
  4. 문서 순서대로 인터리브 → `Vec<ChunkResult>{content, tokens, chunk_order_index}`.
- edgequake가 `ChunkResult` → `TextChunk`(id/offset/line 채움)로 승격하므로 우리는 **content+토큰수+순서만** 반환.
- ⚠️ 모달 원자성은 **전적으로 여기(전략)에서 보장**한다. adaptive_chunk의 어느 엔드포인트도 모달 스팬 원자성 미제공(§3.3-2 검증). 갭 텍스트만 위임.
- 토큰 타깃: adaptive_chunk 기본(1100/600)을 KB 정책으로 고정. edgequake `ChunkerConfig.chunk_size`는 우리 전략이 무시(전략이 직접 자름).

### 3.5 Insert — edgequake (차용)

edgequake `Pipeline`가 우리 청크에 대해: **엔티티/관계 추출 → 임베딩 → 그래프** 수행, 그 결과를 edgequake-api 처리기가 **단일 Postgres(pgvector+AGE)** 에 upsert(증분).

- 임베딩: **BGE-M3 1024d**(`BAAI/bge-m3`, 로컬 OpenAI-호환 서버 :7997; adaptive_chunk도 동일 모델). edgequake 임베딩은 모델/차원 설정가능하나 **fork 수정 2건 필요**(§11): ① `OpenAIProvider`가 미지 모델을 1536 하드코딩 → `with_embedding_dimension`로 `EDGEQUAKE_EMBEDDING_DIMENSION=1024` 적용, ② chat과 분리해 `EDGEQUAKE_EMBEDDING_BASE_URL=:7997/v1`로 임베딩 라우팅. (E2E로 147행 전부 1024d 확인.)
- 추출 LLM: OpenAI-호환으로 **OpenRouter `qwen/qwen3.5-122b-a10b`** 사용(E2E로 158엔티티 추출 확인). ⚠️ edgequake `create_openai()`가 `OPENAI_BASE_URL` 무시+모델 하드코딩하던 버그 → fork에서 `OpenAIProvider::compatible(key, base_url)`+env 모델로 수정(§11).
- 격리: 요청 시 `set_config('app.current_tenant_id', <kb>)` + workspace 헤더 → RLS 자동 적용(§4).

### 3.6 Community — 배치 (신규 W3)

edgequake `detect_communities_guarded`(Louvain, `edgequake-storage`) + 커뮤니티별 **LLM 리포트** 생성.
- **오프라인 배치**(검색 경로와 분리). 트리거: ① 스케줄(야간) 또는 ② 누적 임계(신규 문서 N건/엣지 X%).
- 리소스 가드 존재(대형 그래프 admission 거부, SPEC-006) → KB 규모별 임계 설정 필요(W3).

### 3.7 Search — (차용)

벡터 KNN(pgvector) + 그래프 순회(AGE) + 커뮤니티 리포트 머지. per-KB RLS로 격리. 광역질의는 커뮤니티 리포트, 지역질의는 벡터+엔티티.

---

## 4. 저장소 / 멀티테넌시

- **단일 Postgres**: pgvector(벡터) + Apache AGE(그래프)를 **한 DB**에. 단일 트랜잭션·단일 RLS = 관리포인트 1개. (Qdrant/Memgraph 분리 금지 — §9)
- **per-KB 격리 = 공유테이블 + tenant/workspace 컬럼 + RLS**(KB별 별도 스키마 아님). edgequake가 이 패턴을 이미 구현:
  - `documents/entities/relationships/chunks/...` 에 `ENABLE ROW LEVEL SECURITY` + `tenant_id/workspace_id` 정책(`009`), graph_nodes/edges(`013`), pdf_documents workspace 정책(`022`).
  - 세션 진입 시 `set_config('app.current_tenant_id'/'app.current_workspace_id')` → 정책이 행 자동 필터(앱 버그에도 누출 차단).
- 재처리: 문서 추가=증분 upsert. 전체 재처리는 workspace 단위 `rebuild_knowledge_graph`/`rebuild_embeddings`/ReprocessAll(SPEC-032). (`TaskType::Reindex` 단건 enum만 미구현이며 불요.)

---

## 5. 데이터 계약

### 5.1 블록 스키마 (Blockify 출력)
```jsonc
{"type":"text",     "text":"...", "text_level":1, "page_idx":0}
{"type":"table",    "table_body":"<table>…</table>", "table_caption":[], "table_footnote":[], "page_idx":0}
{"type":"image",    "img_path":"…", "image_caption":[], "page_idx":0}
{"type":"equation", "latex":"…", "text_format":"latex", "page_idx":0}
```

### 5.2 청크 메타데이터 (adaptive_chunk → 적재)
`{doc_id, kb_id, chunk_order_index, page_idx, titles_context, block_type, modal_id?}` — `modal_id`는 모달청크 식별, `source_id`= edgequake가 부여하는 chunk id와 정합.

### 5.3 edgequake 청크 타입 매핑 (W1 핵심)
- 우리 전략 반환: `ChunkResult { content:String, tokens:usize, chunk_order_index:usize }`
- edgequake 승격: `TextChunk { id, content, index, start_offset, end_offset, start_line, end_line, … }`
- **검증필요(W1)**: offset/line은 edgequake가 content에서 계산 → adaptive_chunk가 텍스트를 변형(표 재구조화 등)하면 offset 근사 가능. 정확 스키마는 `edgequake/edgequake/crates/edgequake-pipeline/src/chunker/types.rs` 재확인.

### 5.4 RLS 세션 계약
모든 적재/조회 경로는 시작 시 `set_config('app.current_tenant_id', kb_id)` (+ workspace) 호출 필수. 누락 시 정책이 0건 → "조용한 빈 결과" 버그 주의(W4).

---

## 6. 차용(reuse) vs 신규(build)

**차용**: kordoc/OpenDataLoader/markitdown/VLM(파서) · raganything `modalprocessors` 구조 · `adaptive_chunk` 서비스(기존, 329테스트) · edgequake(`edgequake/edgequake/migrations/*.sql` + `edgequake-pipeline`/`-storage`/`-api`).

**신규(작업항목)**
- **W0 Blockify**: `hybrid_to_blocks()` + VLM `elements[]` 매핑. (Python, 경량)
- **W1 ChunkingStrategy 통합(Rust) — ✅ 완료·E2E 검증(§11)**: `AdaptiveChunkStrategy` 구현(모달 원자성 + 갭→adaptive_chunk HTTP, §3.4) + `Pipeline::with_chunking_strategy()` 신설 + 팩토리 플래그(`EDGEQUAKE_CHUNKER=adaptive`). edgequake fork `edgequake-main`에 merged. (정정: 실 바이너리=루트 `edgequake` bin(`cargo build --bin edgequake`), `edgequake-llm`은 crates.io 의존 0.6.23 → 임베딩차원 수정은 `vendor/edgequake-llm`+`[patch.crates-io]`.)
  - **구현(as built)**: `Pipeline`에 청킹 전략 주입 빌더가 없었기에(`Chunker::with_strategy`만 존재, Pipeline은 `Chunker::new`로 내부 생성) `crates/edgequake-pipeline/src/pipeline/mod.rs`에 `Pipeline::with_chunking_strategy(Arc<dyn ChunkingStrategy>)`를 **신설**했고, 적재 파이프라인 생성부 `crates/edgequake-api/src/workspace_pipeline_factory.rs`(`default_pipeline().with_extractor().with_embedding_provider()`)에서 `EDGEQUAKE_CHUNKER=adaptive`일 때 그 빌더를 호출하도록 배선했다.
  - 추출/임베딩/그래프/persistence/RLS는 edgequake 그대로 차용. default(플래그 미설정) 시 기존 토큰 청커 불변 — E2E로 확인(§11). (대안이었던 "크레이트 링크 별도 서비스"는 미채택.)
- **W2 Modal enrichment**: 모달 블록 LLM 서술(텍스트/비전) + atomic 인라인 + (옵션)앵커 엔티티 등록.
- ✅ **W3 Community 배치** (merged, §11): `kb_pipeline/community.py` — Louvain + qwen 리포트 + `global_query`. kb-pipeline 순수 Python(edgequake 불변).
- ◐ **W4 정합성/RLS** (§8.7/§11): 앱레벨 격리 검증됨. DB레벨 FORCE RLS는 **superuser 롤 우회로 무력** → 프로덕션 하드닝 과제(비-superuser 롤+FORCE RLS+요청당 tx GUC).
- ✅ **W5 Search 머지** (merged, §11): `kb_pipeline/search.py` — local(edgequake vector+graph)/global(community map-reduce) `route` + 워크스페이스 스코프.
- ◐ **W6 파서 라우팅** (§3.1): markitdown은 pptx/DOCX 병합 소실(실측) → 병합 중요시 kordoc/MinerU 라우팅 권고. HWP 계열은 kordoc 신뢰(실측 생략).

---

## 7. 차용할 edgequake 마이그레이션 (스키마 근거)
(경로: `edgequake/edgequake/migrations/`)
`001_init_database`, `009_add_rls_policies`, `013_add_age_graph`, `022_add_pdf_documents_table`(RLS 정책), `028_add_vector_materialized_columns`, `029_add_vector_btree_indexes`, `008_add_multi_tenancy_tables`, `011_tenant_performance_indexes`, `038_*`(tenant/workspace 백필·GIN). + `edgequake/edgequake/docker/init-extensions.sql`(vector/AGE 확장).

---

## 8. 리스크 / 미검증 / 검증 필요
1. ✅ **W1 — 완료·E2E 검증(§11)**: `Pipeline::with_chunking_strategy()` 신설 + 팩토리 배선 구현·merged. 잔여 리스크: edgequake 내부 API 버전 결속 + `vendor/edgequake-llm` 패치 → **upstream 동기화 시 vendor/patch 재적용** 필요(핀 고정 + CI).
2. **HWP/HWPX/HWPML 실측 생략(수용된 리스크, 사용자 결정)**: kordoc 신뢰로 진행. 운영 중 품질 문제 발생 시 사후 검증.
3. **markitdown 병합표 손실**(pptx/DOCX): `<table>=0`, pipe표만 → 병합문서면 정보 손실.
4. **모달 이중추출 회피**: 모달 서술 청크를 edgequake가 추출할 때, (옵션)앵커 엔티티와 중복/충돌 안 나게 dedup 규칙 필요.
5. **커뮤니티 재생성 비용/주기**(W3): 가드 임계 초과 시 거부 → KB 성장 곡선에 맞춘 임계·주기 설계.
6. **offset/line 근사**(§5.3): adaptive_chunk 텍스트 변형 시 lineage 정확도.
7. ◐ **W4 RLS — 앱레벨 격리 검증됨, DB레벨은 프로덕션 하드닝 과제(§11)**: 워크스페이스 격리 실측 확인(별도 벡터테이블+필터, 교차누출 0). 단 **앱이 Postgres superuser 롤(`edgequake`, rolbypassrls=t)로 접속 → FORCE RLS도 무조건 우회**(롤백 tx로 실증) → DB레벨 RLS 현재 무력. 활성화 = (비-superuser 롤+GRANT)+FORCE RLS+요청당 tx GUC(요청=단일 tx)+NULL-tenant 폴백 정리, 결합 all-or-nothing.
8. ✅ **임베딩 모델/차원**: **BGE-M3 1024d**(`BAAI/bge-m3`)를 세 구간(adaptive/edgequake/검색)에서 단일화 — E2E로 147행 전부 1024d 확인(§11). (KURE-v1도 1024d·BGE-M3 계열로 호환.) 운영 메모: bge-m3 main 리비전은 safetensors 부재(torch 2.2.2 CVE로 `pytorch_model.bin` 차단) → safetensors revision 심링크 필요.

---

## 9. 비범위 (Out of Scope)
- 벡터/그래프 store를 Qdrant/Memgraph 등으로 분리(단일 Postgres 원칙 위배, RLS·ACID 상실).
- raganything/LightRAG를 런타임 엔진으로 채택(아이디어만 차용).
- edgequake `TaskType::Reindex` 단건 재색인(workspace rebuild로 대체).
- (가)경로(markdown만 POST)는 MVP 폴백으로만 보존.

---

## 10. Codex 검증 체크리스트
1. ✅(v6) W1 빌더+팩토리 배선 **구현·E2E 검증 완료**(§11) — config 플래그 `EDGEQUAKE_CHUNKER=adaptive`로 선택, default 경로 불변.
2. `ChunkResult`/`TextChunk` 필드(§5.3)와 매핑이 코드와 일치하는지.
3. ◐(v8) RLS 정책은 documents/entities/relationships/chunks/graph 다 덮으나 **앱이 superuser라 미강제**(§11 W4) — `set_config` 주입 경로 부재 확인됨.
4. 커뮤니티 가드 임계·트리거(§3.6)가 운영 가능한 형태인지.
5. ✅(v2 반영) 모달 원자성은 adaptive_chunk가 아니라 **AdaptiveChunkStrategy**가 소유(§3.3/§3.4) — 잔여 검증: 전략의 `〈MODAL〉` 분리 로직이 토큰수/순서 정합을 유지하는지.
6. ✅(v6) 단일 Postgres + **BGE-M3 1024d** 정합 — E2E로 147행 전부 1024d 확인(§11).

---

## 11. 구현·검증 상태 (2026-06-19)

**E2E 검증 통과** — kb_pipeline frontend → edgequake → 추출 → 임베딩 → 검색 전 구간 실동작.
- 구성: LLM=OpenRouter `qwen/qwen3.5-122b-a10b`, 임베딩=`BAAI/bge-m3` 1024d(로컬 :7997 OpenAI-호환), `EDGEQUAKE_CHUNKER=adaptive`.
- 결과(문서 1건): chunk 12, **모달 4개 전부 단일 atomic 청크**(T1→chunk-6, T2→8, T3→9, T4→11), entity 158, relationship 111, 임베딩 147행 전부 1024d, `/api/v1/query` 검색 동작. 0 실패.

**완료(merged)**
- W0 blockify + W2 modal: kb-pipeline `main` (pytest 21).
- W1 AdaptiveChunkStrategy + `Pipeline::with_chunking_strategy()` + 팩토리 플래그: edgequake fork `edgequake-main`. cargo green, 유닛테스트 10.
- kb-pipeline `main`이 edgequake submodule을 머지된 `edgequake-main`에 핀.
- 테스트(main, writable venv `.venv-kb`): **pytest 60 passed** (blockify/modal 21 + community 14 + search 25; 2026-06-19 실행). W6 라우팅 테스트(+14)는 PR#4 브랜치. (codex read-only 샌드박스는 tmpdir 제약으로 pytest 미실행 — 환경 한계, 결함 아님.)

**런타임 중 발견·수정한 edgequake 버그 2건(fork 반영)**
1. 임베딩 차원 미적용: `OpenAIProvider`가 미지 모델을 1536 하드코딩 + `provider_setup`이 `EDGEQUAKE_EMBEDDING_DIMENSION`을 로깅만 함 → `with_embedding_dimension` 추가+적용. `edgequake-llm`이 crates.io 의존(0.6.23)이라 `vendor/edgequake-llm`+`[patch.crates-io]`로 패치.
2. chat provider: `create_openai()`가 `OPENAI_BASE_URL` 무시 + `gpt-5-mini` 하드코딩 → `OpenAIProvider::compatible(key, base_url)` + env 모델 해석으로 수정.

**정정 사실**
- 실 서버 바이너리 = 루트 패키지 `edgequake`(`cargo build --bin edgequake`), `edgequake-api`(lib) 아님.

**W3~W6 (2026-06-19)**
- ✅ **W3 community reports** (merged): `kb_pipeline/community.py` — AGE 그래프→Louvain(python-louvain)→커뮤니티별 qwen 리포트(GraphRAG 프롬프트 이식)→같은 Postgres `community_reports` 저장→`global_query` map-reduce. 라이브: 커뮤니티 60/리포트 15/광역질의가 휴가분류·핵심기준 복원. edgequake Rust 불변.
- ✅ **W5 unified search** (merged): `kb_pipeline/search.py` — `route`(local/global)+워크스페이스 스코프. 라이브 2워크스페이스(휴가/담보신탁): local 정확, global map-reduce, **교차 스코핑 누출 0**.
- ◐ **W4 RLS**: 앱레벨 격리 실측 확인. DB레벨 FORCE RLS는 **앱이 Postgres superuser 롤로 접속해 무조건 우회**되어 현재 무력 → 프로덕션 하드닝 과제(§8.7). 변경 미적용(known-good 유지).
- ◐ **W6 parser routing**: markitdown은 pptx/DOCX 병합표를 파싱시점에 소실(복구불가) → **병합 중요 pptx/DOCX는 kordoc/MinerU 라우팅**(§3.1 반영). 텍스트형은 markitdown.

---

## 부록 A — 근거 (코드 위치)
(edgequake 경로는 `edgequake/edgequake/` 기준)
- ChunkingStrategy 확장점: `crates/edgequake-pipeline/src/chunker/mod.rs:76`(`with_strategy`), `crates/edgequake-pipeline/src/chunker/types.rs:29`(async trait), `crates/edgequake-pipeline/src/pipeline/processing.rs`(process/finish_document_processing).
- RLS: `migrations/009_add_rls_policies.sql`, `migrations/013_add_age_graph.sql`, `migrations/022_add_pdf_documents_table.sql`.
- 텍스트 적재/증분: `crates/edgequake-api/src/processor/text_insert.rs`, `crates/edgequake-api/src/handlers/documents/upload/text_upload.rs`, `crates/edgequake-api/src/handlers/workspaces/bulk_ops/rebuild_knowledge_graph.rs`.
- 커뮤니티: `crates/edgequake-api/src/services/graph_community.rs`, `crates/edgequake-storage/src/community.rs`.
- 프로바이더 설정: `crates/edgequake-api/src/routes.rs:476`(`/settings/providers`; `:471`=`/settings/provider/status`), `crates/edgequake-api/src/provider_types.rs`.
- adaptive_chunk API: `/Users/xxx/workspace/99.projects/adaptive_chunk/service/main.py`(`/chunk`,`/chunk/jobs`), `runner.py`(R1 출력).
- 파서 출력/HTML: `/Users/xxx/workspace/excel-parser-markitdown/compare/adapters/*`(opendataloader `markdown_with_html=True`, ocr `elements[]`), `compare/results.json`.
- 모달 서술: `/Users/xxx/workspace/99.projects/raganything_svc/.venv/.../raganything/modalprocessors.py`(`generate_description_only` → 청크+엔티티 등록), `utils.py:separate_content`.
