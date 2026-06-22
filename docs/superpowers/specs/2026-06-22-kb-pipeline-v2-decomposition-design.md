# kb-pipeline v2 — RAG 수집 플랫폼(facade + 전문 백엔드) 설계 스펙

작성 2026-06-22 · 상태: 설계 확정(대화) → 구현계획
최종 아키텍처: **kb-pipeline = 가벼운 수집 플랫폼 facade**, 그 뒤에 **parse-svc / adaptive_chunk(청킹 허브) / edgequake(저장·검색)** 3개 전문 백엔드를 은닉. 소비자(knowledge_base + 미래 서비스)는 facade capability API만 호출.

## 1. 동기 / 목표

v1 kb_pipeline은 `POST /ingest` 단일 불투명 호출 → phase 안 보임 + 청킹 선택근거 폐기. 또한 **다중 소비자**(knowledge_base + 향후 다른 서비스)가 붙을 로드맵 → 각 소비자가 edgequake/adaptive_chunk에 직접 결합하면 안 됨.

**목표**: kb-pipeline을 **수집 플랫폼 facade**로 만든다 — 안정 capability API(`/parse /chunk /insert /search /ingest`) 뒤에 전문 백엔드를 은닉하고, 정책(워크스페이스·passthrough·chunk_id·chunks_meta)·포맷 변환·오케스트레이션을 facade가 소유. 소비자는 단계별 호출(parse→…) 또는 end-to-end(`/ingest`) 자유 조합. 각 단계는 **API 호출 직전 시작 → 응답 시 완료(✓)** 로 UI에 보인다.

**비목표(v2)**: W4 DB-RLS. (검색은 적재처가 edgequake라 facade `/search`로 포함.)

## 2. 아키텍처 (4계층)

```
[소비자]  knowledge_base(포털·오케스트레이션·UI) + 미래 서비스들
              │  facade capability API (안정 계약)
              ▼
[facade]  kb-pipeline  ── 가벼움: 라우팅 + 정책 + 포맷변환 + 오케스트레이션 (무거운 의존성 X)
              │ 내부 백엔드 호출 (소비자에 은닉)
   ┌──────────┼───────────────────────┬──────────────────┐
[parse-svc]              [adaptive_chunk]            [edgequake(dedicated)]
 W6 파싱+blockify+modal   청킹 허브(adaptive          passthrough chunker +
 (java/OCR/markitdown/qwen) +미래 vl/dify, marker-aware)  임베딩+추출+그래프+검색
```

**계층별 책임 / 진화 축**:
| 계층 | 책임 | 진화 축 | 의존성 |
|--|--|--|--|
| 소비자 | facade capability 조합 + 자기 UI/정책 | 제품별 | — |
| **kb-pipeline facade** | 수집통합: 은닉·정책·포맷변환·오케스트레이션 | 통합 | 가벼움(httpx) |
| **parse-svc** | 실파일 파싱 + modal enrich | 파싱(포맷/모델) | java/OCR/markitdown/qwen |
| **adaptive_chunk** | 청킹 방법선택(허브) + atomic-region(modal) 존중 | 청킹(chunker 추가) | httpx(원격 임베딩) |
| **edgequake** | 임베딩+엔티티추출+그래프+검색 | 저장/검색 | Rust+Postgres |

## 3. facade capability API (kb-pipeline)

소비자에 노출하는 안정 계약. 각 엔드포인트는 **반드시 가치를 더한다(은닉/정책/변환)** — 단순 forward 금지.
| 메서드 | 경로 | 더하는 가치 | 내부 |
|--|--|--|--|
| POST | `/parse` | 파서 라우팅 은닉, 일관 enriched 계약 | → parse-svc |
| POST | `/chunk` | 청킹 허브 은닉, 선택근거 정규화 | → adaptive_chunk |
| POST | `/insert` | **워크스페이스 등록·passthrough join·chunk_id·폴링** 정책 일체 | → edgequake |
| GET | `/insert/status` | edgequake phase 중계(은닉) | → edgequake |
| POST | `/search` | 워크스페이스 스코프·결과 정규화 | → edgequake `/query` |
| POST | `/ingest` | **end-to-end 오케스트레이션**(parse→chunk→insert) — 단계별 호출 원치 않는 소비자용 | → 3 백엔드 |
| POST | `/communities/build` | W3 커뮤니티(은닉) | → edgequake graph |
| GET | `/healthz` | 자신 + 백엔드 헬스 집계 | — |

- 소비자는 **단계별**(`/parse`→`/chunk`→`/insert`, phase 가시) 또는 **`/ingest` 한 방**(간단) 선택.

## 4. Component A — parse-svc (신규 분리 서비스)

위치: `8.kb-pipeline/parse_service`(신규, 기존 `service`의 파싱 로직 이관). v1 `run_front`(parse→blockify→modal) 추출.
| 메서드 | 경로 | 입력 | 출력 |
|--|--|--|--|
| POST | `/parse` | multipart `file`, `filename`, `content_type?` | `{ enriched_content, n_blocks, modal_spans:[{id,type,char_range}] }` |
| GET | `/healthz` | — | `{status, deps:{java,ocr}}` |
- W6 `recommended_parser` 라우팅 + 공유 OCR(:18050)/excel(:18055) 호출 + `kb_pipeline.blockify`/`modal`.
- 무거운 의존성(java/OpenDataLoader 등)은 **여기에만** 격리.

## 5. Component B — adaptive_chunk: 청킹 허브 + marker-aware

위치: `99.projects/adaptive_chunk`(분리 유지). `service/runner.py::run_chunk`.
- **marker-aware(generic atomic-region)**: 옵션 `atomic_markers`(기본 `〈MODAL`…`〈/MODAL〉`, U+3008/U+3009) — 표시된 영역은 **원자 청크**로 보존(쪼개지 않음), 그 사이 gap만 방법선택·청킹 후 **원래 순서로 조립**. modal은 이 generic 기능의 한 용도(adaptive_chunk는 modal 의미를 모르고 "이 영역 원자"만 앎 → 재사용성↑).
- 미종결 마커=텍스트(유실 금지). **마커 0개면 동작 = 기존과 동일(회귀 0)**.
- 반환 R1 형식 `{method_selected, scores, methods_compared, chunks, timing_ms}` 불변(선택근거는 gap 기준, §9 R3).
- 허브 확장성: 향후 vl/dify chunker는 `build_methods`/selector에 추가(별 변경 없이 method로 등록).
- 기존 소비자(knowledge_base adaptive provider :18060 직접) **불변**(분리 유지라 안 깨짐).

## 6. Component C — edgequake fork: passthrough ChunkingStrategy

`crates/edgequake-pipeline/src/chunker/`에 **`PassthroughStrategy`**:
- `chunk(content, _config)`: `\u{001e}`(RECORD SEPARATOR)로 split → 각 조각 `ChunkResult`(빈 제외). 외부호출 없음.
- `EDGEQUAKE_CHUNKER=passthrough` 분기(기존 `adaptive` 보존). 임베딩·추출·그래프 **불변**.
- **재빌드** + `passthrough` 재기동(`search_path=public` 금지 — AGE 깨짐). 단위테스트 split/empty/순서.

## 7. Component D — kb-pipeline facade (기존 service 재구성)

위치: `8.kb-pipeline/service`(facade로 경량화 — 파싱 로직은 parse-svc로 이관). §3 capability 구현, 가벼운 httpx 클라이언트로 3 백엔드 호출.
- `/parse` → parse-svc. `/chunk` → adaptive_chunk(marker 전달). `/insert` → edgequake(기존 `service/edgequake.py`의 ensure_workspace·passthrough join·post_document·document_phase 재사용). `/search` → edgequake `/query`. `/ingest` → 내부 순차.
- **정책 소유**: workspace_id=kb_id→ensure_workspace, passthrough join, chunk_id=`{document_id}-chunk-{i}`, chunks_meta 형식(adaptive_chunk 메타 + edgequake chunk_id).
- v1 `/ingest/submit`·`/ingest/status` 등은 호환 보존하되 v2 capability가 정식.

## 8. Component E — knowledge_base (소비자: 오케스트레이션 + UI)

- **KbPipelineClient**: facade capability 래퍼 — `parse()`, `chunk()`, `insert()`, `insert_status()`, `search()`, `build_communities()`(전부 kb-pipeline facade 호출).
- **`_ingest_kb_pipeline_tail`**: 단계별 오케스트레이션 —
  1. `on_stage("parse")`→`parse()`→enriched
  2. `on_stage("chunk")`→`chunk(enriched, atomic_markers=modal)`→`{chunks, method_selected, scores, methods_compared}`
  3. `on_stage("insert")`→`insert(workspace=kb_id, doc_id, title, chunks)`→document_id; `insert_status` 폴링하며 `on_stage(phase)`
  4. **chunks_meta**(facade가 준 chunk text/titles/pages + chunk_id) `replace_chunks_meta` + **chunking_selection**(`{method_selected,scores,methods_compared}`) `set_chunking_selection`(placeholder 제거) → ready
  - 실패 시 `delete_doc`+failed. 다른 provider tail 불변.
- **워커**(`tasks.py`): kb_pipeline 경로 `on_stage`로 phase별 `set_state`.
- **검색**: kb_pipeline KB는 facade `/search`.
- **프론트** `JobList.tsx`: `STAGE_ORDER = gate→parse→chunk→insert(→extracting/embedding/storing 세부는 insert_status phase)→persist_meta`. 레거시 `select/dify` 제거(fallback 라벨만). 문서상세 `chunking_selection` 카드 = **진짜 method/scores/methods_compared**.

## 9. 데이터 계약 / 리스크
- **계약**: parse→enriched+modal_spans; chunk→`{chunks,method_selected,scores,methods_compared}`; insert→`{document_id,chunk_count,status}`; chunk_id=`{document_id}-chunk-{i}`. workspace_id=kb_id, doc_id=content_hash[:16]. 성공=terminal-ok & chunk_count>0.
- **R1 passthrough 정확성**: `\u{001e}` 경계 보존 + 청크별 임베딩/추출 + chunk_id 순서 일치 라이브 검증.
- **R2 marker-aware 정확성**: adaptive_chunk atomic-region이 edgequake `split_segments`와 동치(원자성·malformed=텍스트·마커0=기존) 단위테스트.
- **R3 선택근거 범위**: gap 텍스트 기준 채점 → modal 대부분 문서는 빈약. UI "(본문 gap 기준)" 주석.
- **R4 chunks_meta 출처**: 표시 메타(titles/pages)는 chunk 응답에서, 검색은 edgequake. chunk_id로 일치.
- **R5 facade 규율**: 각 엔드포인트가 은닉/정책/변환 가치 추가(단순 forward 금지). leaky proxy 방지.
- **R6 운영(5서비스)**: doc_guard/facade/parse-svc/adaptive_chunk/edgequake. 다중소비자 로드맵이 정당화하나 개발환경(16GB) 부담 — 라이브 스모크는 메모리 관리.
- **R7 호환**: adaptive_chunk 기존 호출자·v1 `/ingest`·다른 provider 무영향, 테스트 green.
- **R8 마이그레이션 규모**: v1(단일 service monolith) → facade+parse-svc 분리는 큰 리팩터 → 구현계획에서 **단계 분할**(Phase 1 백엔드 capability[adaptive marker + edgequake passthrough + facade /chunk·/insert], Phase 2 parse-svc 분리, Phase 3 knowledge_base 소비자 전환 + 프론트).

## 10. 테스트
- parse-svc: `/parse` 파싱→modal 계약.
- adaptive_chunk: marker-aware 단위(동치·원자·순서·마커0 회귀) + 방법선택 회귀.
- edgequake: PassthroughStrategy 단위.
- facade: `/chunk`·`/insert`(백엔드 mock) 계약 + 정책(workspace/passthrough/chunk_id) + `/ingest` 오케스트레이션.
- knowledge_base: tail(parse→chunk→insert) 단위 + chunks_meta 병합 + chunking_selection + 검색 + 다른 provider 회귀.
- 라이브 스모크: 실 PDF → 단계 시퀀스(gate→parse→chunk→insert) **각 응답마다 ✓** + 문서상세 **진짜 선택근거** + modal 원자성(표=1청크) + 검색.
