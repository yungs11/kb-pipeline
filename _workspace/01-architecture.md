# 01 · 아키텍처

> 출처: SoT.md §1–5, SoT.v1.md, `docs/kb-pipeline-process-definition.md` §2–5.
> edgequake 실제 Rust 워크스페이스 = `edgequake/edgequake/`(자체 Cargo.toml; crates/·migrations/ 여기). 루트 `edgequake/crates/` 는 별도 crate 세트라 무시.

---

## 1. 전체 파이프라인

```
[1 Parse]        확장자별 최고급 파서 → "markdown + inline HTML 표"
       ↓
[2 Blockify]     hybrid_to_blocks() → 블록 리스트(content_list류)        [parse-svc]
       ↓         (VLM 경로는 서비스 elements[] 를 직접 매핑)
[3 Modal enrich] 표/이미지/수식 블록 → LLM 서술 → content 에 atomic 인라인  [parse-svc]
       ↓         단일 enriched content 스트림 (텍스트 + 서술된 모달, modal_id 마킹)
[4 Chunking]     facade /chunk → adaptive_chunk(atomic_markers) → ChunkResult[]  [facade 소유]
       ↓
[5 Insert]       edgequake: 엔티티/관계 추출 → 임베딩 → 그래프 → 단일 Postgres(pgvector+AGE) 적재(RLS)
       ↓
[6 Community]    edgequake AGE → Louvain detect_communities + LLM 리포트 — **야간 배치 1회**(기본 03:00 KST) + 수동 온디맨드
       ↓
[7 Search]       pgvector KNN + AGE 순회 + community 리포트 머지, per-KB RLS
```

**오케스트레이션 경로**
- **표준(단계별)**: 소비자(kb-backend)가 facade `/parse` → `/chunk` → `/insert`(+poll `/insert/status`)를 직접 순차 호출. (one-shot 아님)
- **블로킹 변형 `/ingest`**: parse→chunk→insert 한 호출 → `{document_id, chunk_count, status, chunking_selection, edgequake_workspace_id}`.
- ~~비동기 변형 `/ingest/submit`~~ **제거(Phase 2d)**: facade 파싱 로직 제거와 함께 `/ingest/submit`·`/ingest/status` 삭제(파싱은 전부 parse-svc 소유). 폴링은 `/insert/status` 로 갈음.

---

## 2. facade (kb-pipeline, :3000) — 오케스트레이터

`service/app.py`. 외부 소비자(knowledge_base 백엔드)에게 capability 를 노출하고 다운스트림을 숨긴다. **청킹과 모달 원자성을 소유**한다.

**노출 capability**: `/parse`, `/chunk`, `/insert`(+`/insert/status`), `/search`, `/ingest`, `/communities/build`. (Phase 2d: `/ingest/submit`·`/ingest/status` 제거 — facade 는 파싱을 안 하고 얇은 orchestration 만 한다. `service/parsing.py`·`excel_parser_client.py`·`ingest.py` 삭제.)

**핵심 책임**
- **워크스페이스 해석 소유**: 모든 핸들러가 `eq.ensure_workspace(workspace_id)` 로 kb id 를 edgequake workspace UUID 로 변환 → 검색·적재를 workspace 스코프로 격리(교차 스코핑 누출 0, 실측).
- **청킹 소유**: `/chunk` 가 adaptive_chunk 허브(:18060)를 모달 마커(`atomic_markers`)와 함께 호출 → 모달 원자성을 facade 에서 강제. (§4 참조)
- **passthrough 묶기**: 적재 시 청크 텍스트를 U+001E(`PASSTHROUGH_SEP`)로 join 해 단일 문서로 만들어 edgequake 에 제출 → edgequake PassthroughStrategy 가 같은 바이트로 분할 → 상류 청커 결과와 1:1 정합.
- **타이밍 passthrough**: `/chunk` 응답에 `timing_details`(adaptive_chunk methods/metrics), `/insert` 응답에 `phases`(edgequake phase 근사) 통과.
- **`edgequake_workspace_id` 반환**: `/insert`·`/ingest` 응답에 포함 → kb-backend 가 `KB.edgequake_workspace_id` 로 영속(그래프 보기 팝업이 X-Workspace-ID 로 edgequake 직접 호출).

---

## 3. parser (parse-svc, :19001) — Parse + Blockify + Modal Enrich

`parse_service/app.py`. 한 번의 `POST /parse` 안에서 FRONT 3단계를 연속 수행해 **enriched_content + 모달 스팬 + 페이지 스팬**을 산출한다. **Phase 2 파서 일원화 완료**: Excel·docx·OCR(pptx/이미지/스캔) 파싱이 전부 parse-svc **in-process** 다(외부 excel-parser :18055 / document-parser :18050 HTTP 미경유). Excel(xlsx/xlsm/xls) 과 CSV 는 `chunk_needed=False` 로 자체청킹 청크를 그대로 반환한다.

### 3.1 Parse — 확장자별 파서 라우팅 (`parse_service/router.py`, 5분기 · 폴백 없음)

라우팅 소유는 `parse_service/router.py`(구 `kb_pipeline.blockify.PARSER_ROUTING`/`recommended_parser` 는 Phase 2d 에서 삭제). markitdown 은 코드·requirements 에서 완전 제거(재유입 가드 `parse_service/tests/test_no_markitdown.py`).

| 확장자 | 도메인 | 파서(in-process) | `<table>` HTML | `chunk_needed` | 비고 |
|--------|--------|------------------|----------------|----------------|------|
| PDF | `pdf` | **페이지수준 라우팅**(triage) → 페이지마다 odl / paddle_gw / vl / skip (아래 §3.1.1) | ✅ (06=70개, pipe=0) | True | JRE 21 의존(ODL). 문서당 .md 1개 → `<<<ODL_PAGE_BREAK>>>` sentinel 로 페이지 복원. 라우팅: `parse_service/parsers/pdf/gate.py`. 게이트웨이 실패 시 그 페이지만 VL 전사 |
| XLSX/XLSM/XLS | `excel` | **excel_parser_rag**(vendored, `auto`→전결 openpyxl / 그 외 kordoc) | ✅ | **False** | LLM 없이 parse+chunk 결합 → native 청크. `chunk_strategy=excel_rag_parser` |
| DOCX·HWP·HWPX·DOC·PPT·PPTX·HTML | `pdf` | **원격 변환 API → PDF → ODL/GW/VL** | ✅ | True | `run_parse` 가 변환 후 `.pdf` 이름으로 PDF 레인에 넣는다(2026-08-06) |
| 단일 이미지 | `ocr` | **in-process VL OCR**(`parsers/ocr`) | ✅ + elements[] | True | 이미지→base64 → **PAGE_HYBRID**(전사 + 시각 서술) VL 호출. `IMAGE_EXTS`={png,jpg,jpeg,gif,bmp,tif,tiff,webp} |
| TXT·MD·CSV·JSON | `text` | 그대로 블록화(utf-8-sig/utf-16/cp949) | — | True | 변환·파서 없음. `page_count=1` |
| 그 외(폴백) | `fallback` | **kordoc** CLI | ✅ | True | 미지 확장자(hwpx 등)는 kordoc 로(구 markitdown 폴백 제거) |
| 스캔 페이지 | (pdf 내부, `paddle_gw` 레인) | **PaddleOCR-VL 게이트웨이**(원격 GPU — layout + VL 인식 + 표 조립) | ✅ | True | **그 페이지만 전송**(`page_numbers` 부분집합, 2026-08-12). 페이지 이미지 1장씩 `/tasks` 비동기 호출. **layout 이 image/figure/chart 를 (면적 5%↑) 검출한 스캔 페이지는 전면 VL 로 교체하고 paddle 표는 승계**(Plan A — `_hybrid_scan_pages`). 미설정/레인 불능/엔진 사고 시 **그 페이지만 VL 전사 폴백** |
| 가로형·다이어그램·혼합콘텐츠 페이지 | (pdf 내부, `vl` 레인) | **in-process VL 전면 전사**(PAGE_HYBRID) | ✅ | True | `LLM_NEEDED` 페이지 전부(2026-08-12 Phase 2a). 표·본문 원문전사 + 순서도 흐름서술 + 차트 요약을 **한 프롬프트**에서 처리 — 표 승계 없이 페이지 전체를 VL 이 읽는다 |
| VL 전사 폴백 | (pdf 내부) | **네이티브 텍스트**(`RenderedPage.text`) | — | True | VL 전사 실패(절단·모델 퇴화) 시 PyMuPDF 추출본으로 폴백. 재시도는 무효였다(실측 회복률 0%, `temperature=0.1`). 폴백 체인 재설계는 Phase 2b |

#### 3.1.1 PDF 페이지수준 라우팅 (2026-08-12 Phase 2a — 문서수준 게이트 대체)

**문서수준 레인이 사라졌다.** 이전에는 문서 전체가 odl / vl / paddle_gw 중 하나로 갔는데,
그림 비율(`KBP_GATE_VL_RATIO`)만 보고 문서 전체를 VL 로 넘기던 경로에서 **표가 많은 문서가
표 테두리 벡터선(curve=350)을 순서도로 오탐당해 통째로 재전사되며 표가 깨졌다**(KIS 11p 실관측).
이제 **페이지마다** 자기 신호대로 레인을 고르고 병합한다.

| triage bucket | 레인 | 처리 |
|---|---|---|
| `SKIP` | `skip` | ODL md 있으면 블록화, 없으면 빈 blocks. **VL 미호출** |
| `OCR_NEEDED` | `KBP_GATE_OCR_LANE`(기본 `paddle_gw`) | 게이트웨이 + layout 기반 hybrid |
| `LLM_NEEDED` | **`vl`** | 가로형·다이어그램·세로형 혼합콘텐츠 **전부**. 페이지 전체를 VL 이 전사 |
| `TEXT_ONLY` | `odl` | ODL 이 표·텍스트 보존. 빈약하면 그 페이지만 VL 전사 |

`_page_lane` 은 **`is_landscape` 를 보지 않는다** — `triage.py` 가 이미 가로형을 `LLM_NEEDED`
로 마킹하므로(`KBP_TRIAGE_LANDSCAPE_TO_LLM`, 기본 1) 매핑 하나로 따라온다.

**감수하는 교환(명시)**: `LLM_NEEDED` 인 모든 페이지가 "정확한 ODL 네이티브 텍스트" 대신
"VL 전사" 를 받는다. 사용자가 근거를 보고 선택했다. 부작용으로 `narrate_pages` 가 거의 항상
공집합이 되어 **`_supplement_diagram_pages` 는 사실상 사문**이다(제거는 Phase 4).

**탈출구**: `KBP_GATE_OCR_LANE=vl` 로 두면 스캔 페이지가 게이트웨이 대신 VL 전사로 간다 —
게이트웨이가 아예 없는 폐쇄망용. `verify-bundle.sh:106/115` 두 가드가 이 값에 걸려 있다.

**게이트 배치**: v1 GW quarantine 게이트는 `_supplement_diagram_pages` **뒤**, 그리고
**`paddle_pnos - demoted - hybrid_replaced` 부분집합에만** 돈다. 전량을 넘기면 phase 2
mutation 이 odl/vl 페이지의 ODL 네이티브 본문까지 지운다.

**강등(demote)은 엔진 사고뿐**이다 — 레인 불능(프로브 실패·URL 공란) 또는 페이지
`status == "error"`. `status == "ok"` + 빈 blocks 는 강등하지 않고 게이트가 판정한다:
그 집단이 v1 이 측정한 "게이트가 잡은 페이지" 이고 거기서 VL 은 **구조율 0 · 날조 2건**
이었다(Fisher p=0.021).

**`paddle_gw` 페이지 dict = 6-key 계약**
`(page_number, blocks, layout, page_size, status, error)`. 두 브랜치가 서로 다른 4-tuple 을
내고 있었고, 어느 한쪽만 취하면 조용히 죽는다 — `layout` 소실 → hybrid 가 영구 거짓,
`status` 소실 → 게이트웨이 페이지 실패가 demote 도 VL 도 못 받고 빈 페이지.

| PDF | `pdf` | **문서수준 게이트**(triage) → 순수텍스트=**OpenDataLoader**(`markdown_with_html=True`) / 스캔·혼합=**MinerU**(hybrid-http-client) | ✅ (06=70개, pipe=0) | True | JRE 21 의존(ODL). 문서당 .md 1개 → `<<<ODL_PAGE_BREAK>>>` sentinel 로 페이지 복원. 게이트: `parse_service/parsers/pdf/gate.py` (triage 버킷 집계). MinerU 는 지연 import(로컬 미설치 허용), 실패 시 ODL/VL 폴백 |
| XLSX/XLSM/XLS/**CSV** | `excel` | **excel_parser_rag**(vendored, `auto`→전결 openpyxl / 그 외 kordoc) | ✅ | **False** | LLM 없이 parse+chunk 결합 → native 청크. `chunk_strategy=excel_rag_parser`. csv 는 헤더 서식을 준 xlsx 로 메모리 합성해 **openpyxl 고정**(2026-08-11, `parsers/excel/csv_to_xlsx.py`) |
| DOCX·HWP·HWPX·DOC·PPT·PPTX | `pdf` | **원격 변환 API → PDF → ODL/GW/VL** | ✅ | True | `run_parse` 가 변환 후 `.pdf` 이름으로 PDF 레인에 넣는다(2026-08-06). html 은 2026-08-11 이 경로에서 빠졌다 |
| **HTML/HTM** | `html` | **`parsers/html`**(bs4 + markdownify, 형변환 API 미경유) | ✅ (원본 `<table>` 보존) | True | 최상위 표를 nonce sentinel 로 빼고 나머지만 markdown 화 후 복원. data-URI `<img>` 는 alt 로 대체(2026-08-11) |
| 단일 이미지 | `ocr` | **in-process VL OCR**(`parsers/ocr`) | ✅ + elements[] | True | 이미지→base64 → **PAGE_HYBRID**(전사 + 시각 서술) VL 호출. `IMAGE_EXTS`={png,jpg,jpeg,gif,bmp,tif,tiff,webp} |
| TXT·MD·JSON·LOG·XML | `text` | 그대로 블록화(`tools/textdecode.py`) | — | True | 변환·파서 없음. `page_count=1`. BOM utf-32/utf-16 → utf-8-sig → cp949. XML 은 2026-08-11 편입. 블록 0개면 `parse_failed`(조용한 빈 적재 금지) |
| 그 외(미지 확장자) | `pdf` | — | — | — | `domain_of` 가 pdf 로 보내고 `app.py` 의 `%PDF` 가드가 `not a PDF (and not convertible)` 로 거절한다. 별도 폴백 파서 없음 |
| 스캔/혼합 PDF | (pdf 내부, MinerU 레인) | **MinerU** hybrid (PaddleOCR 로컬 layout + **VLM 원격**) | ✅ | True | 게이트가 `OCR_NEEDED` 하나라도 있으면 `parse_method='ocr'` 강제(스캔 텍스트 유실 방지), 스캔 없는 텍스트+이미지 혼합만 `'auto'`. content_list→`elements_to_blocks`. VLM=`MINERU_VLM_SERVER_URL`(별도 GPU). 미설정/실패/빈결과 시 **ODL/in-process VL 폴백**(가용성). 배포서버 전용(`docs/mineru-deploy-notes.md`) |
| 스캔 PDF 폴백 | (pdf 내부) | **in-process VL OCR** | ✅ | True | MinerU 미가동(로컬/실패) 시 폴백. 글자 거의 없는 페이지만 렌더 → VL OCR 보충(best-effort 비치명) |

- **표준 중간표현**: "markdown + inline HTML 표". 표는 절대 pipe 로 납작화 금지.
- 문서 ID 폴백 = `sha256(file_bytes).hexdigest()[:16]`(orchestrator 동일 식 → MinIO 키 일치). 파일명 정규화 = `tools.safe_basename`(경로 탈출 차단, 구 `parsing._safe_basename` 이동). 비표시문자 제거 = PUA(U+E000–U+F8FF).
- **VL OCR 계약(in-process, `parsers/ocr/vl_api.py`)**: OpenAI 호환 chat/completions(`MODEL_API_URL`/`MODEL_API_KEY`/`MODEL_NAME`), guided-json(`GUIDED_JSON_MODE=response_format` OpenRouter 호환), 응답 스키마 `elements[].{category(table|figure),content{html,markdown,text}}`. 동시성 `KBP_VL_MAX_CONCURRENT`(기본 8), 페이지 실패 비치명. 순수 텍스트 figure 는 text 블록으로 재분류(markdown 유실 방지).

> **⚠️ OCR 실제 origin = 원격 VL(`MODEL_API_URL`) + 스캔 게이트웨이(`KBP_PADDLE_OCR_GATEWAY_URL`), `:18050` 아님 — `KBP_OCR_URL` 은 dead (2026-07-27 코드 대조 확정, 제거됨)**
> 흔한 오해: parse-svc `healthz` 가 (구) `{"deps":{"ocr":"http://localhost:18050"}}` 를 표시하고 `run_parse` 가 `ocr_url` 을 라우터→PDF/OCR 파서로 스레딩하므로 "파싱이 :18050 을 탄다"고 착각하기 쉽다. **실제로는 안 탄다.** 최종 소비자 `ocr_elements_sync`(`parsers/ocr/__init__.py:88`, `prompt_override` 만 받음)·`_ocr_elements_for_page`(`parsers/pdf/__init__.py:43-50`, 3번째 인자는 diagram 프롬프트 override 이지 URL 아님) 가 **`ocr_url` 을 전혀 소비하지 않는다**. excel 도 `excel_url 은 하위호환용 무시 파라미터`(`parsers/excel/__init__.py:46` 주석).
> - **실 OCR 경로 2개**: (a) pptx·단일이미지·스캔폴백 = **in-process VL**(`vl_api.py:243,263` → `MODEL_API_URL`, 현재 `openrouter.ai/.../chat/completions`, qwen3-vl). (b) 스캔/혼합 PDF 본류 = **paddle_gw 게이트웨이**(`KBP_PADDLE_OCR_GATEWAY_URL` = `15.164.81.29:18081/ocr/paddleocr_vl`(2026-08-13 이관 — 구 `api-doc.ys-helperai.com`), gate.py 가 스캔 판정 시 위임). 이 둘이 live origin.

> - **실 OCR 경로 2개**: (a) pptx·단일이미지·스캔폴백 = **in-process VL**(`vl_api.py:243,263` → `MODEL_API_URL`, 현재 `openrouter.ai/.../chat/completions`, qwen3-vl). (b) 스캔/혼합 PDF 본류 = **paddle_gw 게이트웨이**(`KBP_PADDLE_OCR_GATEWAY_URL` = `http://15.164.81.29:18081/ocr/paddleocr_vl` — 2026-08-11 교체, 이전 api-doc.ys-helperai.com — gate.py 가 스캔 판정 시 위임). 이 둘이 live origin.
> - **정리 완료(2026-07-27)**: `scripts/parse-svc.env` 의 `KBP_OCR_URL`/`KBP_EXCEL_URL` 삭제, `healthz.deps` 를 `{"vl_ocr": MODEL_API_URL}` 로 정정(`app.py:359`), `run_parse(ocr_url="", excel_url="")` 로 dead 값 표식(`app.py:379`). 파서 시그니처의 `ocr_url`/`excel_url` 파라미터 자체는 테스트 대량 의존이라 하위호환 유지(무시 인자).
> - 로컬 `:18050`(`trust-backend-document-parser-1`, docker `0.0.0.0:18050→8000`)은 **별개 스택(dify/trust-backend)** 컨테이너로 이 파이프라인과 무관. 그 컨테이너 healthz 초록은 kb-pipeline OCR 정상성 근거가 **아니다**.
> - **OCR 장애 진단 지점**: `MODEL_API_URL`(OpenRouter) + `KBP_PADDLE_OCR_GATEWAY_URL`. (별개로 청킹 auto 스코어링·검색·적재 임베딩은 `litellm.ax-demo.com` bge-m3 — 또 다른 origin.)

### 3.2 Blockify — `hybrid_to_blocks()` / `elements_to_blocks()`

`kb_pipeline/blockify.py`. "markdown + inline HTML 표"를 **블록 리스트**로 변환(`markdown-it-py(html=True).enable("table")`):
- `html_block` + `<table` → `{type:"table", table_body:<HTML 그대로>, page_idx}`
- `heading_open` → `{type:"text", text, text_level:N, page_idx}`
- `table_open`(GFM pipe표) → HTML 렌더 → table 블록
- `<img>`/`![]()` → image, `$$`/math → equation, 그 외 → text
- **VLM 경로 예외**: 서비스 `elements[]`(category=text/table/image/equation/title)를 직접 블록으로 매핑(가장 충실).

규칙: ① 블록 1개 = 구조 단위 1개(문서 순서 유지), ② 표는 HTML 보존, ③ `page_idx` 부여, ④ `text_level` 로 섹션 계층 유지.

**page_idx / 페이지 이미지**: canonical 1-based. OCR elements 보통 0-based → `page_number = page_idx + 1` 정규화. 페이지 이미지는 `parse_service/pdf_pages.py:render_pdf_pages`(PyMuPDF, dpi=300, jpg q=90)로 JPEG 래스터화 → MinIO 키 `{docs_id}/{docs_id}_{p}.jpeg`. MinIO 미설정 시 업로드만 skip.

### 3.3 Modal Enrich — 표/그림/수식 LLM 서술

`kb_pipeline/modal.py:enrich_with_spans`. raganything `modalprocessors` 의 **구조만** 차용(엔진 채택 아님). 모달 블록마다:
- **table/equation**: HTML/LaTeX → **텍스트 LLM** 서술(비전 불요)
- **image/figure**: `img_path` → **비전 LLM** 서술

**합류 방식(고급안: 독립 모달청크 + 엔티티)**: 서술을 atomic 마킹 블록으로 인라인.
```
〈MODAL id="T1" type="table"〉[앞문맥]\n{LLM 서술}\n{table_body HTML}\n[뒤문맥]〈/MODAL〉
```
- 마커 괄호 = U+3008/U+3009, **W1 Rust 소비자와 byte-identical**.
- `[앞문맥]`/`[뒤문맥]` 의 의미는 경로별로 다르다 — **LLM on = 흡수한 제목/각주 원문**(원본 블록은 사라짐), **LLM off(기본) = 앞 블록 끝 ≤200자·뒤 블록 앞 ≤100자 *사본***(원본 블록은 그대로 남음, 요약이 빈 문자열이라 `[앞문맥]\n\n{payload}`).
- 창 크기 `BEFORE_WINDOW=3`/`AFTER_WINDOW=6`(복사 경로에선 '스캔 범위' — 이 안의 **첫 비공백 블록 1개**만 복사), 병렬 워커 `KBP_MODAL_MAX_WORKERS`(기본 3), id 카운터(표 `T`/수식 `E`/그림 `I`).
- **LLM on**: 두 모달이 같은 사이 블록을 다투면 **문서순 앞 모달이 선점**. LLM 실패 시 해당 모달만 흡수 0·요약 생략으로 강등(폴백, 재시도 없음).
- **LLM off(복사)**: consume 이 없어 선점 미적용 — `[표1][X][표2]` 의 X 는 **3중 등장**(표1 뒤문맥·표2 앞문맥·원본). 원본의 페이지 귀속 불변, 사본만 표 페이지에 계상.
- **oversize 2단계**: 추정치 > `_OVERSIZE_CHARS`(13800) 이면 ①복사 문맥만 버려 래핑 유지 → ②본체만으로도 초과면 bare.
- 산출: `enriched_content` + `modal_spans`(`[{id,type,char_range:[start,end]}]` 반열림) + `page_spans`(`[{page_number,char_start,char_end}]`). 세그먼트 join 은 `\n\n`(2자), 이 길이를 running offset 에 반영해 page_spans 계산.

> **기본 동작(중요)**: 모달 LLM 보강은 `KBP_MODAL_ENRICH` 로 토글하며 **기본 off("0")**. off 일 때 LLM 0회로 각 모달을 `summary="", tc=fc=0`(흡수 0) 으로 강등해 원본 payload 를 마커로 통과하되, 문맥은 **복사**한다(앞 ≤200자·뒤 ≤100자 사본, 원본 블록 생존). 모달 원자성·page_spans 는 유지되어 청킹/페이지 지표 무영향, 손실은 표/그림 검색용 의미요약뿐. 현재 `/parse` 는 `vision_llm=None` 이라 그림은 LLM 미호출.

→ 표/그림이 **검색가능 텍스트 + 그래프 노드**로 승격되며, 그래프는 edgequake 추출이 단독 생성(이중생성 없음).

---

## 4. chunker (adaptive_chunk, :18060) — facade 가 소유

> **현행(v2):** 청킹은 **facade `/chunk` 가 소유**한다. facade 가 `adaptive_chunk /chunk` 를 `atomic_markers=〈MODAL〉…〈/MODAL〉` 로 호출해 **모달 원자성까지 facade 에서 강제**한다(adaptive_chunk `service/runner.py` `_segment_atomic`/`DEFAULT_ATOMIC_MARKERS`). 따라서 전용 edgequake 는 `EDGEQUAKE_CHUNKER=passthrough`. (구버전 SoT 의 "adaptive_chunk 엔드포인트가 모달 atomic 미강제" 메모는 atomic_markers 추가로 무효화됨. v1 의 edgequake 내부 `AdaptiveChunkStrategy` 경로 → §변경 02 참조.)

**facade `/chunk` 처리**
- facade 가 모달 마커 `MODAL_ATOMIC_MARKERS=[["〈MODAL","〈/MODAL〉"]]` 를 잡 본문 `options.atomic_markers`(최상위 필드 아님)로 전달. 허브는 모달 *의미*를 모른 채 "이 스팬은 원자적"이라는 사실만 받아 단일 atomic 청크로 유지(marker-aware chunking).
- 나머지 텍스트 갭만 **4방법 경쟁**으로 청킹.
- 대형 입력·느린 방법 대응으로 **비동기 잡**: `POST /chunk/jobs` → `GET /chunk/jobs/{id}` 폴링(간격 3s, 폴링 타임아웃 1800s, 클라 timeout 600s). terminal: `succeeded`/`failed`/`cancelled`.
- 정규화: 허브 R1 `chunk_text`→`text`, `chunk_pages`→`pages`. 산출 `chunks[]`(`chunk_index/text/titles_context/pages`) + `chunking_selection`(`method_selected`, `scores`, `methods_compared`).

**4방법 경쟁(완화는 비범위, 사용자 결정으로 현상 유지)**
- `recursive` (recursive_1100/600), `llm_regex`(reasoning LLM 단일콜 ~339s 실측), `semantic`(문장쌍 N-1 reranker), `coref`(RC LLM). **승자 1개 선택에 전부 지불** → 청커 ~10분의 원인(03-dev-progress 참조).
- 토큰 타깃 = 허브 기본 1100/600(KB 정책 고정).

**청크 메타 계약**: `{doc_id, kb_id, chunk_order_index, page_idx, titles_context, block_type, modal_id?}`. `modal_id`=모달 청크 식별자, `source_id`=edgequake 부여 chunk id 와 정합.

---

## 5. edgequake (:3001) — 베이스 엔진 (차용)

Rust, `crates/edgequake-pipeline`. passthrough 로 facade 청크를 받아 추출·임베딩·그래프 적재·검색 수행.

### 5.1 Insert 파이프라인
`service/edgequake.py:EdgequakeClient` → edgequake `Pipeline`. 제출 `POST /api/v1/documents`(`async_processing:true`):
1. `chunk_async`(**PassthroughStrategy**, U+001E 분할) — 상류와 1:1 복원
2. `extract_parallel` — 엔티티/관계 추출 (LLM=OpenRouter `qwen/qwen3.5-122b-a10b`)
3. `finish_document_processing` — link_extractions_to_chunks → `generate_all_embeddings`(bge-m3 1024d) → `build_lineage`

폴링 `GET /api/v1/tasks/{track_id}`, 단계 `pending→chunking→extracting→embedding→indexing/storing→completed`, poll_timeout 1200s/간격 3s. 산출 `{document_id, chunk_count, status}`(성공=`indexed`).

- **격리**: `set_config('app.current_tenant_id', <kb>)` + `X-Workspace-ID`/`X-Tenant-ID`. tenant 기본 `00000000-0000-0000-0000-000000000002`, workspace `kb-<kbid>` 슬러그 `ensure_workspace`(멱등). 테이블 `eq_eq_default_ws_<short8>_vectors`, 그래프 `eq_eq_default_graph`.
- **재청킹 금지**: 반드시 `EDGEQUAKE_CHUNKER=passthrough`. `adaptive` 로 띄우면 이중청킹 → 빈 구분자 조각 → HTTP 422 적재 실패.
- **문서단위 그래프 추출 스킵(2026-06-30)**: 2단계 `extract_parallel`(엔티티/관계)은 문서 단위로 끌 수 있다. facade `/insert` 의 `extract_graph=false`(UI 라디오, **엑셀은 고정 미추출**) → submit 본문 `metadata.skip_graph_extraction=true` → edgequake `process_with_resilience_cancellable_opts(skip_extraction)` 가 추출 서브블록만 건너뛴다. 1·3단계(청킹·임베딩·lineage)는 그대로라 벡터검색 무영향. 0엔티티여도 status=completed(skip 가드). 상세 02-changes §0-A.

### 5.2 Community — Louvain + 리포트 (검색과 분리된 오프라인 배치)
`kb_pipeline/community.py:build_workspace_communities`(순수 Python, edgequake Rust 불변). 두 경로 기동 — (1) facade `POST /communities/build` 온디맨드(202 + 백그라운드, 예외 swallow), (2) global 검색 시 build-if-missing.
1. `fetch_graph`(`eq_eq_default_graph` Node/EDGE, workspace_id 스코프, `properties::text::jsonb`)
2. `build_communities`(networkx + python-louvain `best_partition`, weight, `random_state=42`)
3. `generate_report`(Entities/Relationships CSV → GraphRAG `COMMUNITY_REPORT_PROMPT`, `/no_think` 접두 → JSON)
4. `store_reports`(`public.community_reports` upsert, `ON CONFLICT (workspace_id, level, community_id)`)

산출 행 = `title, summary, findings(jsonb), rank, entity_ids[]`. 라이브 실측: 커뮤니티 60 / 리포트 15.

### 5.3 Search — 명시 mode(배선됨) + 자동 라우터(미배선)
- **local(기본)** — facade `POST /search` `{mode:"local"}`(또는 생략): edgequake hybrid 질의(`POST /api/v1/query`, 벡터 KNN + 그래프 순회 서버측 머지) → `{answer, results}`(`results[]`={chunk_id,text,score,document_id}). 기존 동작 그대로.
- **global(배선됨)** — `{mode:"global", global_top_k:1~5}`: `kb_pipeline.search.global_search` 를 직접 호출 → 커뮤니티 리포트 map-reduce. 응답에 `community_reports_ready`·`report_newest_at`·`report_oldest_at`·`report_count` 를 함께 싣는다.
  - **동시성은 DB 카운터로 제한**한다(`kbp.global_search_slots` + `pg_advisory_xact_lock`). map N + reduce 1 의 **순차** LLM 이라 요청 하나가 `(N+1)×timeout` 을 점유하는데, gunicorn `-w 4` 에서 `threading.Semaphore` 는 프로세스마다 따로 세어 전역 상한이 되지 못한다. 상한 초과 = 즉시 503(대기 없음).
  - **LLM 타임아웃은 이 경로만 별도**다 — `KBP_GLOBAL_LLM_TIMEOUT`(기본 60s) vs 적재 경로 `KBP_LLM_TIMEOUT`(300s). 300s 를 그대로 쓰면 한 요청이 최악 30분을 점유한다.
  - `reports_exist` 가 **테이블 부재는 "없음"(fail-open), 그 외 psycopg 오류는 raise** 한다 — DB 장애를 "리포트가 아직 없다" 는 거짓 안내로 위장하면 안 되기 때문. app.py 가 그 예외를 503 으로 바꾼다.
- **자동 라우터(여전히 미배선)** — `kb_pipeline/search.py:unified_search` + `route()`(GLOBAL_CUES 단서어 + tiny LLM 타이브레이크). **의도적으로 배선하지 않았다**: 사용자가 버튼으로 모드를 고르는 편이 오판(휴리스틱이 넓은 질문을 local 로 보내는 것)보다 낫고, 오판 비용이 최대 6분 LLM 이다. app.py 는 `global_search` 만 import 한다.

---

## 6. 저장소 / 멀티테넌시 (단일 Postgres, :5433)

- **단일 Postgres**: pgvector(벡터) + Apache AGE(그래프)를 **한 DB**에. 단일 트랜잭션·단일 RLS = 관리포인트 1개. (Qdrant/Memgraph 분리 금지 — 비범위)
- **per-KB 격리 = 공유테이블 + tenant/workspace 컬럼 + RLS**(별도 스키마 아님). edgequake migrations: `009_add_rls_policies`(documents/entities/relationships/chunks), `013_add_age_graph`(graph_nodes/edges), `022_add_pdf_documents_table`(workspace 정책).
- 세션 진입 시 `set_config('app.current_tenant_id'/'app.current_workspace_id')` → 정책이 행 자동 필터. workspace 헤더(`x-workspace-id`/`x-tenant-id`)로 `TenantContext::from_headers` 가 워크스페이스 벡터 테이블 격리.
- 모든 검색 모드가 application-level 에서 workspace_id 로 제약 — 교차 스코핑 누출 0(실측).
- 재처리: 문서 추가=증분 upsert. 전체 재처리는 workspace 단위 `rebuild_knowledge_graph`/`rebuild_embeddings`/ReprocessAll(SPEC-032). `TaskType::Reindex` 단건은 미구현·불요.
- ⚠️ **W4 RLS 한계**: 앱이 superuser 롤(`edgequake`, rolbypassrls=t)로 접속 → FORCE RLS 도 무조건 우회. 앱레벨 격리는 검증됨, DB레벨은 프로덕션 하드닝 과제(02/03 참조).

---

## 7. 데이터 계약

### 7.1 블록 스키마 (Blockify 출력)
```jsonc
{"type":"text",     "text":"...", "text_level":1, "page_idx":0}
{"type":"table",    "table_body":"<table>…</table>", "table_caption":[], "table_footnote":[], "page_idx":0}
{"type":"image",    "img_path":"…", "image_caption":[], "page_idx":0}
{"type":"equation", "latex":"…", "text_format":"latex", "page_idx":0}
```

### 7.1-B PageTrace — 페이지별 관측 (Phase 2b-1, 2026-08-13)

`/parse` 응답의 **`page_traces`** — "이 페이지가 어느 레인으로 갔고, **무엇이 최종 blocks 를
만들었고**, 무슨 일이 있었나". `page_verdicts`(게이트 대상 **부분집합**)와 **공존**한다.

```
{"page_number", "bucket", "lane", "source", "attempts", "chars",
 "verdict", "state", "verdict_reason"}
```

**`source` 어휘** — 무엇이 최종 blocks 를 만들었나:

| 값 | 뜻 |
|---|---|
| `gw` | 게이트웨이 산출물 그대로 |
| `gw_hybrid` | hybrid 가 전면 VL 로 교체 — **`gw` 와 구분 필수**(내용이 게이트웨이 것이 아니다) |
| `vl` | VL 전사 |
| `gw_fallback` | **폴백 체인 1단계** — VL 실패 후 게이트웨이가 채움 (2026-08-14 신설) |
| `vl_md_fallback` | **폴백 체인 2단계** — ODL md 가 채움 (2b-2 가 지웠다가 2026-08-14 복원) |
| `native_fallback` | **폴백 체인 3단계** — PyMuPDF 네이티브 텍스트가 채움 (동상) |
| `odl_md` | odl 레인 정상 경로 |
| `skip` | SKIP 페이지 — **내용 없음이 정상** |
| `unclassified` | 방어코드. 값이 나오면 **라우팅 버그 신호** |
| **`empty`** | 어느 경로도 못 구했다 = **품질 상한 지표** |

**VL 폴백 체인**(`KBP_VL_FALLBACK_CHAIN`, 기본 ON — 2026-08-14 사용자 확정):
`VL 실패 → pw → odl → rp.text → 빈 페이지`. **이미 거친 레인은 건너뛴다**(paddle_gw 출신은
pw, odl 출신은 odl). 끄면 2b-2 동작 — VL 실패가 곧 문서 실패이고 **품질은 안 떨어진다**
(다른 출처로 채우지 않으므로 열화가 가려지지 않는다).

근거(실측 2026-08-14): 전량 VL 문서가 프로바이더 절단으로 **간헐 40% 실패**(온톨로지 18p
5회 중 2회, p17 이 3회 다 truncated). 문서 결함이 아니라 프로바이더 사정이라 재실행하면
통과한다. 체인 ON 재측정에서 **3/3 성공**하고 그중 1회는 p17 을 `gw_fallback` 이 살렸다.

**stage 어휘**: `vl` · `odl` · `hybrid_vl` · **`gw`**(2026-08-14 신설 — 게이트웨이 시도가
그전까지 `attempts` 에 한 줄도 안 남았다). `_fail_if_vl_empty` 는 `a[0]=="vl"` ∧
`meta.attempt` 만 보므로 `gw`·`hybrid_vl` 은 판정에서 자동 제외된다.

⚠️ **`skip`·`unclassified` 는 `empty` 로 덮지 않는다**(`_EMPTY_IS_NORMAL`) —
정상적으로 비는 경로와 실패를 섞으면 "품질 상한" 이 거짓이 된다.
게이트 quarantine 도 `source` 를 바꾸지 않는다(blocks 를 비울 뿐 — `verdict` 로 표현).

**`source` 확정 시점 = `RouteResult` 생성 뒤**. 병합 루프 뒤에 blocks 를 바꾸는 곳이 셋이다:
`_supplement_diagram_pages`(append) · 게이트 quarantine(비움) ·
**`filter_degenerate_pages`(`parse()` 안, 생성 **뒤** → `_refresh_trace_sources` 가 갱신)**.

**`attempts`** — `(stage, outcome, meta)`. `meta` 에 `model`·`tokens`·`finish`.
**`finish` 가 처방을 가른다**: `length`(상한 소진 → 상한을 올린다) vs
`stop`+짧은 응답(서빙이 스스로 끊음 → 서빙을 본다). `provider` 는 안 남긴다(폐쇄망 자체 서빙).

### 7.2 청크 메타데이터 (chunk → 적재)
`{doc_id, kb_id, chunk_order_index, page_idx, titles_context, block_type, modal_id?}` — `modal_id`=모달청크 식별, `source_id`=edgequake chunk id 와 정합.

### 7.3 RLS 세션 계약
모든 적재/조회 경로는 시작 시 `set_config('app.current_tenant_id', kb_id)`(+workspace) 호출 필수. 누락 시 정책 0건 → "조용한 빈 결과" 버그 주의.

### 7.4 임베딩 일관성 (BGE-M3 1024d)
- 청킹·적재·검색 세 구간을 `bge-m3` 1024차원으로 단일화.
- **현행 운영 배선 = 원격 litellm**: `EDGEQUAKE_EMBEDDING_PROVIDER=openai`, `EDGEQUAKE_EMBEDDING_BASE_URL=https://litellm.ax-demo.com/v1`, `EDGEQUAKE_EMBEDDING_MODEL=bge-m3`, `EDGEQUAKE_EMBEDDING_DIMENSION=1024`(`service/scripts/start_dedicated_edgequake.sh`). 로컬 `:7997` 은 과거 스모크 기록의 대체 구성.
- 임베딩 BASE_URL 은 chat(추출) LLM 과 분리(임베딩=litellm, chat=OpenRouter qwen). (KURE-v1 도 1024d 호환.)
- 운영 메모: bge-m3 main 리비전은 safetensors 부재(torch 2.2.2 CVE) → safetensors revision 심링크 필요.

### 7.5 차용할 edgequake 마이그레이션
(경로 `edgequake/edgequake/migrations/`) `001_init_database`, `008_add_multi_tenancy_tables`, `009_add_rls_policies`, `011_tenant_performance_indexes`, `013_add_age_graph`, `022_add_pdf_documents_table`, `028_add_vector_materialized_columns`, `029_add_vector_btree_indexes`, `038_*`(tenant/workspace 백필). + `docker/init-extensions.sql`(vector/AGE 확장).

---

## 8. Excel 전용 경로 — excel_parser_rag (in-process, Phase 2b)

Excel(xlsx/xlsm/xls)은 parse-svc `parsers/excel` 이 vendored **excel_parser_rag** 패키지를 **in-process** 로 직접 호출해(구 excel-parser :18055 HTTP 제거) **LLM 없이** parse+chunk 를 함께 수행하고 native 청크를 `chunk_needed=False` 로 반환한다(`chunk_strategy=excel_rag_parser`). 백엔드는 `EXCEL_PARSER_BACKEND`(이미지 기본 `auto`): 전결 문서 → openpyxl, 그 외 → kordoc(.md). kordoc CLI 는 parse-svc 이미지에 `npm install -g kordoc` 로 내장(`KORDOC_BIN`). 표는 `<table>` HTML 보존.

- **레거시 `.xls`(BIFF)는 레인 입구에서 `.xlsx` 로 변환**(2026-08-13) — `parsers/excel/__init__.py`
  가 **매직바이트**(`\xD0\xCF\x11\xE0` = OLE CFB)를 보고 soffice(LibreOffice, parse-svc 이미지
  내장)로 바이트를 갈아끼운다. csv 와 같은 자리·같은 방식이다. **확장자로 판정하지 않는다** —
  이름만 `.xls` 인 xlsx(zip)는 오늘도 정상 처리되므로 변환하면 되던 게 죽고, 이름이 `.xlsx` 인
  진짜 BIFF 는 확장자 기준으로 못 잡는다. 입구에서 바꾸므로 하류(백엔드 3종·게이트·청킹)는
  코드 변경이 없고, 전결(Tier1)·계층(Tier1.5) 라우팅의 확장자 게이트도 자연히 통과한다.
  ⚠️ **LibreOffice 는 캐시된 오류값을 소문자로 쓴다**(`#REF!` → `#ref!`) — 그래서 게이트의
  `ERROR_RE` 가 `re.IGNORECASE` 다. 이게 없으면 `.xls` 문서에서 참조오류 검사가 통째로 침묵한다.
- **csv 의 청킹 소유는 엑셀 레인**(2026-08-11) — csv 는 헤더 행에 서식(볼드+채우기)을 준 xlsx 로 메모리 합성돼(`parse_service/parsers/excel/csv_to_xlsx.py`) `chunk_needed=False` 로 자체 청킹된다. facade `/chunk` 를 타지 않는다. 백엔드는 **openpyxl 고정** — `auto` 는 전결 키워드나 계층 지배도가 없으면 kordoc 으로 떨어지는데 csv 유래 평면 표는 둘 다 아니고, `KORDOC_BIN` 없는 환경에선 실패한다. 헤더 서식이 없으면 `header_detector` 의 style gate 에 걸려 청크가 `사번: 1001` 대신 `A: 1001` 로 퇴화한다. **한계**: 첫 컬럼 헤더가 계층 spine 용어(`항목`·`구분`·`품명` 등)를 포함하면 그 열의 키 이름이 `A:` 로 떨어진다(값은 청크 경로에 보존) — `deferred.md` D45.

> Phase 2e 로 외부 excel-parser/document-parser/redis 컨테이너는 compose 에서 제거됨 — 파싱 fleet 은 parse-svc 단일 이미지(java+node/kordoc+PyMuPDF)로 통합. office/hwp→PDF 는 원격 변환 API(2026-08-06, gotenberg 제거).

---

## 부록 A — 코드 레퍼런스 색인

> `docs/kb-pipeline-process-definition.md`(프로세스정의서 v1.0) §5.7 의 코드 사실을 단계별로 요약. 운영 배선의 권위 출처는 기동 런처 `service/scripts/start_dedicated_edgequake.sh` 와 facade `service/app.py`, parse-svc `parse_service/app.py`, `kb_pipeline/*` 모듈이다.

| 단계 | 진입점 / 함수 | 핵심 식별자 |
|------|---------------|-------------|
| **Parse** | `POST /parse` → `run_parse()` → `parse_to_pages()` | doc_id 폴백 `sha256(bytes)[:16]`, 파일명 `_safe_basename`, 비표시문자 `_PUA_RE`(U+E000–F8FF). PDF sentinel `_PAGE_SEP`=`<<<ODL_PAGE_BREAK>>>`. 산출 `list[PageDoc]`(`{page_number(1-based), blocks}`) |
| **Blockify** | `kb_pipeline.blockify`: `hybrid_to_blocks(md, page_idx)`, `elements_to_blocks(elements)` | math `$$..$$`→equation, `<img>`/`![]()`→image, heading→`text_level`. OCR category 매핑. page_idx 1-based |
| **페이지 이미지** | `parse_service.pdf_pages.render_pdf_pages` | PyMuPDF/`fitz` lazy, dpi=300, jpg q=90. MinIO 키 `{docs_id}/{docs_id}_{p}.jpeg`(`page_uuid="{docs_id}_{p}"`) |
| **Modal Enrich** | `parse_service/app.py:run_parse` → `kb_pipeline.modal.enrich_with_spans`, LLM=`service/llm.py:get_text_llm` | `BEFORE_WINDOW=3`/`AFTER_WINDOW=6`, `KBP_MODAL_MAX_WORKERS`(기본 3), 마커 `〈MODAL id="X" type="..."〉…〈/MODAL〉`(U+3008/U+3009). 산출 `enriched_content`+`modal_spans`(반열림)+`page_spans`. 토글 `KBP_MODAL_ENRICH`(기본 off) |
| **Chunking** | facade `/chunk` → `service/adaptive_chunk.py:AdaptiveChunkClient` | 마커 `MODAL_ATOMIC_MARKERS=[["〈MODAL","〈/MODAL〉"]]` → 잡 본문 `options.atomic_markers`. 비동기 `POST /chunk/jobs`→`GET /chunk/jobs/{id}`(간격 3s, 폴타임아웃 1800s, 클라 600s). 정규화 `chunk_text`→`text`, `chunk_pages`→`pages`. join `chr(0x1E)`(`PASSTHROUGH_SEP`) |
| **Insert** | `service/edgequake.py:EdgequakeClient` + edgequake `Pipeline` | `POST /api/v1/documents`(`async_processing:true`). `chunk_async`(PassthroughStrategy)→`extract_parallel`(qwen)→`finish_document_processing`(link→`generate_all_embeddings` bge-m3 1024d→`build_lineage`). 폴링 `GET /api/v1/tasks/{track_id}`(또는 `document_phase`), poll_timeout 1200s. tenant 기본 `00000000-0000-0000-0000-000000000002`, ws `kb-<kbid>` `ensure_workspace`. 테이블 `eq_eq_default_ws_<short8>_vectors`, 그래프 `eq_eq_default_graph` |
| **Community** | `kb_pipeline/community.py:build_workspace_communities`; 트리거는 **둘뿐** — (1) **야간 배치** `service/community_schedule.py`(facade-worker 데몬 스레드, 기본 03:00 KST, `kbp.graph_touch` 를 근거로 그래프가 변한 workspace 만 큐에 넣는다), (2) 수동 `POST /communities/build`(202→잡 큐). **적재 tail 트리거는 제거됐다**(A1) | `fetch_graph`(`properties::text::jsonb`)→`build_communities`(networkx+python-louvain `best_partition`, `random_state=42`)→`generate_report`(`COMMUNITY_REPORT_PROMPT`, `/no_think`)→`store_reports`(`public.community_reports` upsert, `ON CONFLICT (workspace_id, level, community_id)`) |
| **Search(실)** | `service/app.py:search` → `eq.ensure_workspace` → `eq.search(workspace_id, query, top_k)` | edgequake `POST /api/v1/query`(hybrid), top_k→max_results. 산출 `{answer, results[{chunk_id,text,score,document_id}]}` |
| **Search(global, 실)** | `service/app.py:search` `mode="global"` → `_search_global` → `kb_pipeline.search.global_search` | `global_top_k` 1~5(clamp). 슬롯 `kbp.global_search_slots`+advisory lock(`KBP_GLOBAL_SEARCH_CONCURRENCY`, 기본 2, `0`=완전 차단), 슬롯 TTL=`(top_k_max+1)×timeout×2`. LLM 타임아웃 `KBP_GLOBAL_LLM_TIMEOUT`(60s). psycopg 오류→503, httpx 오류→422(kb 클라 재시도 증폭 방지 — 429/5xx 만 재시도한다) |
| **Search(자동 라우터, 미배선)** | `kb_pipeline/search.py:unified_search`, `route()` | `GLOBAL_CUES`+tiny LLM 타이브레이크. **의도적 미배선** — 명시 mode 토글로 대체(오판 비용이 최대 6분 LLM). app.py 미import |
