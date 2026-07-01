# kb-pipeline v2 — 라이브 통합 스모크 런북 (Task 4.1)

검증 일시: 2026-06-22 · PDF: `test_doc/3-3. 휴가규정(2025.12.05. 개정).pdf`

## 1. 서비스 기동 (8개)

| 서비스 | 포트 | 기동 메모 |
|--|--|--|
| eq-pg-kbp (Postgres) | 5433 | docker, POSTGRES_PASSWORD=edgequake_secret |
| edgequake (passthrough) | 8081 | `EDGEQUAKE_CHUNKER=passthrough`, `DATABASE_URL=postgres://edgequake:edgequake_secret@localhost:5433/edgequake` (search_path 옵션 **금지** — AGE 보존). debug 바이너리에 PassthroughStrategy 포함 확인. |
| bge-m3 | 7997 | 임베딩 1024d |
| adaptive_chunk (marker-aware) | 18060 | `.venv/bin/uvicorn service.main:app`. **재기동 필수**(2026-06-18 기동본은 Task 1.1 marker-aware 코드 미반영 → 표 분할 버그). 재기동 후 `atomic_markers` 옵션 노출 확인. |
| parse-svc | 19001 | `.venv-kb/bin/uvicorn parse_service.app:app`. `KBP_OPENAI_API_KEY`(.env), `KBP_OCR_URL=:18050`, openjdk@17 PATH. |
| facade (kb-pipeline) | 19000 | `.venv-kb/bin/uvicorn service.app:app`. **재기동 필수**(이전 기동본은 v2 `/parse /chunk /insert /search` 라우트 미존재 → 404). 재기동 후 12개 라우트 노출. |
| doc_guard | 8000 | |
| knowledge_base | 8088 | facade `:19000` 바라봄(config.kb_pipeline_base_url). |

기동 순서 주의: 코드가 디스크에서 최신이어도 **이미 떠 있던 프로세스는 구버전 코드**일 수 있음 → facade·adaptive_chunk 재기동으로 v2 코드 로드.

OpenRouter 키: `/Users/xxx/workspace/99.projects/rag-edgequake-benchmark/docker/.env` 의 `OPENAI_API_KEY` — **절대 출력 금지**.

## 2. PRIMARY — facade 단계별 적재 (parse→chunk→insert)

워크스페이스: 신규 UUID `44847d19-e75c-4635-b399-7f4da8a29ec9`
doc_id = content_hash[:16] = `0746f73066b7aed5`

- **/parse** (54s): `n_blocks=83`, `enriched_content` 10227자, `modal_spans=4`(전부 table), 〈MODAL/〈/MODAL〉 마커 4/4 균형.
- **/chunk** (21s): `method_selected=recursive_600`(진짜), `scores`={sc:1.0, icc:0.80, dcc:0.87, ba:0.58, avg:0.81}, `methods_compared`=[recursive_1100 0.714, recursive_600 0.814]. 총 12청크 — **modal 표 4개가 각각 정확히 1청크(원자), unbalanced 0**.
- **/insert** (43s): `document_id=be693e30-...`, **chunk_count=12(>0)**, `status=indexed`(terminal-ok).
- **/search** (5s): 진짜 근거 한국어 답변 + 186 sources(top hit = `be693e30-...-chunk-0` score 2.04).

### SQL 검증 (per-workspace 벡터 테이블 격리)
- 문서는 **단 하나**의 워크스페이스 테이블 `eq_eq_default_ws_7682fbe7_vectors`에만 존재(나머지 ws 테이블 0행).
- 그 테이블 `workspace_id` 단일값(`7682fbe7-...`) → 물리 격리.
- chunk_id 행 12개: `{document_id}-chunk-0..11` (계약 `{document_id}-chunk-{i}` 일치). 나머지 158행 = 엔티티/관계 임베딩(추출 산출물).
- **modal 표 청크 4개**(chunk-6/8/9/11)만 metadata에 MODAL 포함 → 저장 단계까지 표 원자성 보존.

## 3. one-shot /ingest 오케스트레이션
신규 ws → `chunk_count=12`, `status=indexed`, `chunking_selection.method_selected=recursive_600`(진짜 scores+methods_compared 포함). SQL: 12 chunk_rows / 4 modal_chunks.

## 4. SECONDARY — knowledge_base 소비자
풀 UI e2e는 인증(401) + 백그라운드 워커 필요 → 환경 부담으로 미수행(anti-loop 준수). 대신:
- Phase-3 단위테스트 **26 passed**: KbPipelineClient / tail 오케스트레이션(parse→chunk→insert, 진짜 chunking_selection) / 워커 stage / chat 검색 배선 / provider accept.
- kb backend config `kb_pipeline_base_url=http://localhost:19000`(facade) 확인.
- facade 라이브 경로(위 §2~3)로 소비자가 호출할 capability를 실증.

## 5. 결과
PRIMARY 전부 PASS. 계약(workspace_id=kb_id, doc_id=content_hash[:16], chunk_id={document_id}-chunk-{i}, U+001E passthrough, 〈MODAL…〈/MODAL〉 원자성) 라이브 검증 완료. OpenRouter 키 미출력.
