<!-- plan-version: v5 -->
<!-- codex-validation: READY v5 at 2026-06-18T08:52:45Z -->
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
| pptx | **markitdown** | md(pipe표) | ❌ `<table>=0` | 표 단순 가정. 병합시 소실(W6) |
| DOCX | **markitdown** | md(pipe표) | ❌ | 동일. 사용자 실측상 일반문서 우세 |
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

- 임베딩: **KURE-v1 1024d**(한국어; `nlpai-lab/KURE-v1`, KURE 서버 :7997 — adaptive_chunk·raganything_svc가 이미 사용). edgequake는 임베딩 **모델/차원이 설정가능**(provider config; 내장 예시는 mistral-embed/mxbai-embed-large 등)하므로, 워크스페이스 `embedding_model/embedding_dimension`를 **KURE-v1 1024d에 핀**한다(pgvector 2000d 한계 내). 즉 edgequake 내장 모델이 아니라 우리가 지정하는 모델.
- 추출 LLM/프로바이더: `/api/v1/settings/providers`로 설정(하드코딩 아님, 검증됨). 로컬/OpenRouter 선택.
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
- **W1 ChunkingStrategy 통합(Rust) — 중간 규모 포크**: `AdaptiveChunkStrategy` 구현(모달 원자성 + 갭→adaptive_chunk HTTP, §3.4) + edgequake 파이프라인 생성 경로에 결선.
  - ⚠️ **검증결과(이 repo의 `edgequake/edgequake/` 직접 확인)**: 프로덕션 적재 경로는 `Pipeline::default_pipeline()`를 **팩토리/부트스트랩 계층에서 생성**하며 `ChunkingStrategy`가 text_insert까지 **현재 안 꽂혀 있다.** "insert 핸들러 1-파일 수정"이 아니라 다음 다중파일 수정이 필요(경로=`edgequake/edgequake/` 기준):
    - `crates/edgequake-api/src/workspace_pipeline_factory.rs:93` — `Pipeline::default_pipeline().with_extractor(..).with_embedding_provider(..)` 생성. **`with_strategy` 없음** ← 주입 핵심 지점.
    - `crates/edgequake-api/src/state/query_bootstrap.rs:13`(`build_ingestion_pipeline`)→`:19`(`default_pipeline()`).
    - `crates/edgequake-api/src/processor/workspace_resolver.rs:22/66`(`get_workspace_pipeline[_strict]`) → `crates/edgequake-api/src/processor/text_insert.rs:165`가 이 파이프라인을 받아 `process` 호출.
    - 기타 `default_pipeline()` 생성처(모두 `crates/edgequake-api/src/` 하위): `state/query_runtime.rs:56`, `state/memory.rs:253`, `state/mod.rs:303`, `processor/mod.rs:298`.
  - ⚠️ **추가 발견**: `crates/edgequake-pipeline/src/pipeline/mod.rs`의 `Pipeline` 빌더에는 `with_extractor`/`with_embedding_provider`만 있고 **청킹 전략 주입 빌더가 없다**(`Chunker::with_strategy`는 `Chunker`에만 존재, Pipeline은 내부에서 `Chunker::new`로 생성). 따라서 W1은 **`Pipeline::with_chunking_strategy(Arc<dyn ChunkingStrategy>)` 빌더 신설**(pipeline crate 수정) + 위 팩토리 생성처에서 그 빌더 호출(config 플래그)까지 포함한다.
  - 범위: 추출/임베딩/그래프/persistence/RLS는 그대로 차용. **수정 표면 = pipeline crate 빌더 신설 + 팩토리/부트스트랩 청커 배선.** (중간 규모 포크.)
  - 대안 = 크레이트 링크 별도 서비스(분리 깔끔하나 persistence/RLS 배선 재현 필요 → 더 큼).
- **W2 Modal enrichment**: 모달 블록 LLM 서술(텍스트/비전) + atomic 인라인 + (옵션)앵커 엔티티 등록.
- **W3 Community 배치**: Louvain+리포트 트리거(스케줄/임계) + 가드 임계 설정.
- **W4 정합성/RLS 운용**: 단일 Postgres 트랜잭션 경계 + RLS 세션 주입을 모든 경로에 누락없이 + 삭제/갱신 시 고아 엔티티/커뮤니티 무효화.
- **W5 Search 머지**: 벡터+그래프+커뮤니티 결합 + RLS 적용(상당부분 edgequake 재사용, 튜닝).
- **W6 파서 검증**: pptx/DOCX 병합표 손실 평가(필요시 pipe→HTML 승격 or 대체파서). (HWP/HWPX/HWPML은 kordoc 신뢰 → 실측 생략, 사용자 결정.)

---

## 7. 차용할 edgequake 마이그레이션 (스키마 근거)
(경로: `edgequake/edgequake/migrations/`)
`001_init_database`, `009_add_rls_policies`, `013_add_age_graph`, `022_add_pdf_documents_table`(RLS 정책), `028_add_vector_materialized_columns`, `029_add_vector_btree_indexes`, `008_add_multi_tenancy_tables`, `011_tenant_performance_indexes`, `038_*`(tenant/workspace 백필·GIN). + `edgequake/edgequake/docker/init-extensions.sql`(vector/AGE 확장).

---

## 8. 리스크 / 미검증 / 검증 필요
1. **W1 Rust 결속 — pipeline crate 빌더 신설 + 팩토리 배선**: `Pipeline`에 전략 주입 빌더가 없어 `Pipeline::with_chunking_strategy()`를 신설하고, 팩토리/부트스트랩 생성처(workspace_pipeline_factory:93, query_bootstrap:13, query_runtime/memory/mod, workspace_resolver→text_insert:165)에서 호출해야 함(중간 규모 포크). edgequake 내부 API에 버전 결속 → 포크 핀 고정 + CI. (경로=이 repo `edgequake/edgequake/` 기준.)
2. **HWP/HWPX/HWPML 실측 생략(수용된 리스크, 사용자 결정)**: kordoc 신뢰로 진행. 운영 중 품질 문제 발생 시 사후 검증.
3. **markitdown 병합표 손실**(pptx/DOCX): `<table>=0`, pipe표만 → 병합문서면 정보 손실.
4. **모달 이중추출 회피**: 모달 서술 청크를 edgequake가 추출할 때, (옵션)앵커 엔티티와 중복/충돌 안 나게 dedup 규칙 필요.
5. **커뮤니티 재생성 비용/주기**(W3): 가드 임계 초과 시 거부 → KB 성장 곡선에 맞춘 임계·주기 설계.
6. **offset/line 근사**(§5.3): adaptive_chunk 텍스트 변형 시 lineage 정확도.
7. **RLS 세션 누락 위험**(W4): `set_config` 빠지면 조용한 빈 결과 → 미들웨어로 강제.
8. **임베딩 모델/차원 고정**: **KURE-v1 1024d(우리 지정; edgequake는 설정가능)** 를 세 구간(adaptive/edgequake/검색)에서 단일화 확인.

---

## 9. 비범위 (Out of Scope)
- 벡터/그래프 store를 Qdrant/Memgraph 등으로 분리(단일 Postgres 원칙 위배, RLS·ACID 상실).
- raganything/LightRAG를 런타임 엔진으로 채택(아이디어만 차용).
- edgequake `TaskType::Reindex` 단건 재색인(workspace rebuild로 대체).
- (가)경로(markdown만 POST)는 MVP 폴백으로만 보존.

---

## 10. Codex 검증 체크리스트
1. ✅(v3 반영, in-repo `edgequake/edgequake/` 직접확인) W1 = `Pipeline::with_chunking_strategy()` 빌더 신설 + 팩토리 배선(workspace_pipeline_factory:93 등)으로 재산정 — 잔여 검증: 신설 빌더가 default_pipeline 경로와 충돌 없이 config 플래그로 선택되는지.
2. `ChunkResult`/`TextChunk` 필드(§5.3)와 매핑이 코드와 일치하는지.
3. RLS 정책이 `chunks`·graph까지 덮는지, `set_config` 주입 경로(§4)가 실재하는지.
4. 커뮤니티 가드 임계·트리거(§3.6)가 운영 가능한 형태인지.
5. ✅(v2 반영) 모달 원자성은 adaptive_chunk가 아니라 **AdaptiveChunkStrategy**가 소유(§3.3/§3.4) — 잔여 검증: 전략의 `〈MODAL〉` 분리 로직이 토큰수/순서 정합을 유지하는지.
6. 단일 Postgres 원칙과 KURE 1024d 정합이 edgequake 설정으로 충족되는지.

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
