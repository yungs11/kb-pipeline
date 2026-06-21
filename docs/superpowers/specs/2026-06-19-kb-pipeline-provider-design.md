# kb-pipeline as a knowledge_base provider — 설계 스펙

작성 2026-06-19 · 상태: 승인됨(설계) → 구현계획 단계

## 1. 목표 / 범위 (v1)

shinhan_trust의 `knowledge_base` 서비스에 **kb-pipeline을 새 provider로 추가**한다. KB 생성 시 provider=`kb_pipeline`을 고르면, 문서 업로드가 우리 파이프라인(W0~W3, W6)을 타서 적재되고 청크가 화면에 보인다.

**v1 포함**: 업로드 → doc_guard(기존 전역) → 파싱 → blockify → modal 서술 → adaptive 청킹 → 임베딩 → 적재 → `chunks_meta` → document detail 화면에 청크 표시 → (적재 성공 후) 커뮤니티 리포트 빌드(W3, 비차단 잡).

**v1 비포함**: 통합검색(W5) UI/엔드포인트(추후), DB레벨 RLS 하드닝(W4, 별도 과제).

## 2. 아키텍처 (2-part + 전용 edgequake)

```
[knowledge_base :8001 (FastAPI)]                  [kb-pipeline 서비스 :19000 (신규, 8.kb-pipeline)]
 POST /kb/{kb}/documents (업로드)
   → Stage0 doc_guard (전역, 기존)
   → ingest_document() provider 분기
   → provider=="kb_pipeline":
        _ingest_kb_pipeline_tail() ──HTTP──▶ POST /ingest
                                                파싱(W6 라우팅) → blockify → modal 서술
                                                → [전용 edgequake :8081 (우리 fork, adaptive)]
                                                    AdaptiveChunkStrategy → adaptive_chunk(:18060)
                                                    + 임베딩(bge-m3 :7997) + 적재(Postgres pgvector+AGE)
        ◀── chunk_meta ── GET /chunks ◀────────── (전용 edgequake에서 청크 조회)
   → repo.replace_chunks_meta → status=ready
   → (성공시) arq 잡: community build ──HTTP──▶ POST /communities/build (async, qwen)
 GET /kb/{kb}/documents/{doc} → chunks_meta → [Next.js :3100 document detail 화면에 청크 표시]
```

## 3. Component A — kb-pipeline HTTP 서비스 (신규)

위치: `8.kb-pipeline` repo, `service/` (FastAPI). 기존 `kb_pipeline` 패키지(blockify/modal/community/search) 재사용.

### 3.1 엔드포인트
| 메서드 | 경로 | 입력 | 출력 |
|--|--|--|--|
| POST | `/ingest` | multipart: `file`, `workspace_id`, `doc_id`, `content_type?` | `{document_id, chunk_count, status}` |
| GET | `/chunks` | `workspace_id`, `doc_id` | `[{chunk_id, text, hierarchy_path, page_number}]` |
| DELETE | `/doc` | `workspace_id`, `doc_id` | `204` |
| POST | `/communities/build` | `workspace_id` (async/202) | `{job_id|status}` |
| GET | `/healthz` | — | `{status:"ok", edgequake, bge_m3}` |

### 3.2 /ingest 내부 흐름
1. **파싱**(확장자별, W6 `recommended_parser` 라우팅): pptx/docx→structural(kordoc/MinerU), pdf→OpenDataLoader, xls/xlsx/hwp계열→kordoc, image/scanned→VLM(:18050). 공유 OCR(:18050)·excel(:18055) 서비스 재사용. 출력 = "markdown + inline HTML 표".
2. **blockify** `hybrid_to_blocks()` → 블록 리스트.
3. **modal** `enrich(blocks, text_llm=qwen, vision_llm=...)` → 표/수식은 qwen(텍스트)으로 서술, 이미지 블록은 OpenRouter 비전 모델 설정 시 서술 / 없으면 v1에선 스킵. `〈MODAL〉` atomic 인라인 enriched content (실제 LLM 서술, stub 아님).
4. **적재**: enriched content를 전용 edgequake `POST /api/v1/documents`(`X-Workspace-ID`/`X-Tenant-ID`)로 적재. 전용 edgequake는 `EDGEQUAKE_CHUNKER=adaptive`라 AdaptiveChunkStrategy가 `〈MODAL〉` atomic + 갭→adaptive_chunk(:18060) → 임베딩(bge-m3) → chunk/entity/embedding 저장.
5. **반환**: `{document_id, chunk_count, status="completed"|"failed"}`.

### 3.3 /chunks
전용 edgequake/Postgres에서 해당 doc의 청크 조회 → `chunk_id, text, hierarchy_path, page_number`로 매핑(기존 `edgequake_client.fetch_chunk_meta` 로직 차용 가능).

### 3.4 전용 edgequake 인스턴스
- 우리 fork 바이너리(`edgequake/edgequake/target/.../edgequake`)를 **별도 포트 :8081**, `EDGEQUAKE_CHUNKER=adaptive`, OpenRouter qwen + bge-m3(:7997)로 기동.
- DB: **전용 Postgres 컨테이너 :5433**(edgequake-postgres 이미지, pgvector+AGE) — 기존 vanilla edgequake(:8080) DB와 완전 분리. edgequake 기동 시 마이그레이션 자동 적용.
- knowledge_base의 기존 `edgequake`(vanilla, :8080) provider는 불변.

## 4. Component B — knowledge_base provider 통합 (전부 additive)

기존 코드 수정 최소(분기/검증/드롭다운 1줄씩), 나머지는 신규 파일.

| # | 변경 | 종류 |
|--|--|--|
| B1 | `backend/app/clients/kb_pipeline_client.py` (KbPipelineClient: `ingest`/`fetch_chunk_meta`/`delete_doc`/`build_communities`) | 신규 |
| B2 | `core/pipeline.py`: `KbPipelineLike` Protocol | 신규(추가) |
| B3 | `core/pipeline.py`: `PipelineDeps.kb_pipeline: KbPipelineLike \| None` | 추가 |
| B4 | `core/pipeline.py`: `_ingest_kb_pipeline_tail(...)` (raganything tail 미러) | 신규 함수 |
| B5 | `core/pipeline.py`: `ingest_document()`에 `if kb.provider=="kb_pipeline": return _ingest_kb_pipeline_tail(...)` | 1블록 추가 |
| B6 | `config.py`: `kb_pipeline_base_url=:19000`, `kb_pipeline_timeout_seconds`, `kb_pipeline_max_retries` | 추가 |
| B7 | `dependencies.py`: `build_pipeline_deps()`에서 KbPipelineClient 빌드 → `PipelineDeps.kb_pipeline` | 추가 |
| B8 | `routers/kb.py`: provider 검증 튜플에 `"kb_pipeline"` 추가 | 1줄 |
| B9 | `frontend/app/kb/page.tsx`: 드롭다운 `<option value="kb_pipeline">kb-pipeline (내부 파이프라인)</option>` | 1줄 |
| B10 | 커뮤니티(W3): `_ingest_kb_pipeline_tail` 성공 후 arq 잡 enqueue → KbPipelineClient.build_communities (비차단) | 추가 |

### 4.1 tail 계약 (raganything 패턴 일관)
- workspace_id = kb_id, doc_id = `content_hash[:16]`.
- 성공 판정: `outcome.succeeded == (status=="completed" and chunk_count>0)`.
- 실패 시 `delete_doc`로 정리 + status=failed. 교체(replacement) 시 old doc_id 삭제.
- 성공 시 `persist_success` + `replace_chunks_meta(rows=fetch_chunk_meta 매핑)` + status=ready.

## 5. 데이터 흐름 (end-to-end)
업로드 → (기존)blob 저장+ingestion_job → 워커 `ingest_document` → Stage0 doc_guard(전역) → Stage1~3 dedup/문서행/청킹결정(기존 공통) → **provider==kb_pipeline tail** → kb-pipeline `/ingest`(파싱→블록→모달→전용edgequake adaptive 적재) → `/chunks` → `replace_chunks_meta` → ready → 화면 표시 → (성공)community build 잡.

## 6. 핵심 계약/스키마
- `chunks_meta` 매핑: `chunk_id`(edgequake chunk id), `text`(청크 본문, 〈MODAL〉 포함 가능), `hierarchy_path`(titles_context), `page_number`(있으면), `quality_score`(옵션). 기존 chunks_meta 스키마 재사용.
- 임베딩 1024d(bge-m3), LLM qwen/qwen3.5-122b-a10b — 전용 edgequake 환경에 고정.

## 7. 배포/포트
| 구성 | 포트/위치 | 비고 |
|--|--|--|
| kb-pipeline 서비스 | :19000 (신규) | FastAPI, 8.kb-pipeline/service |
| 전용 edgequake(adaptive) | :8081 (신규) | 우리 fork |
| 전용 edgequake Postgres | :5433 (신규) | edgequake-postgres 이미지, vanilla(:8080) DB와 분리 |
| 기존 edgequake(vanilla) | :8080 (불변) | knowledge_base 기존 provider |
| adaptive_chunk | :18060 (기존) | 청킹 백엔드 |
| bge-m3 | :7997 (기존) | 임베딩 |
| OCR/VLM, excel | :18050 / :18055 (기존) | 파싱 공유 |
| knowledge_base | :8001 / 프론트 :3100 | 기존 |

## 8. 테스트
- kb-pipeline 서비스: `/ingest`·`/chunks` 계약 테스트(mock edgequake), 실파일 파싱 경로 1건 스모크(test_doc).
- 통합 스모크: knowledge_base에서 KB(provider=kb_pipeline) 생성 → test_doc 업로드 → doc_guard 통과 → 청크가 document detail에 표시 → 커뮤니티 잡 생성 확인.
- 격리: kb_pipeline provider가 기존 edgequake(:8080)/dify/raganything provider에 영향 0(additive).

## 9. 리스크 / 비범위
- **실파일 파싱 경로**: 지금까지는 사전파싱(compare/out) 사용 → 서비스는 실파일을 파서로 구동해야 함(파서 어댑터 래핑 필요, 리스크 중간).
- **전용 edgequake DB**: 별도 DB/마이그레이션 + 기동 스크립트 필요.
- **커뮤니티 비용**: qwen 직렬 → 반드시 비차단 잡(업로드 응답 블로킹 금지).
- 비범위: W5 검색 UI, W4 DB-RLS 하드닝, edgequake 바이너리 재빌드(머지본).
