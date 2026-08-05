<!-- design-version: rev4 -->
<!-- ultracode-validation: READY (rev4, 2026-07-03; rev3 verdict READY + 2 minor 하드닝 반영) -->

# 배선 설계 — parse → doc_guard(PII) → chunk

- 작성일: 2026-07-03 (rev4 — rev3 ultracode verdict **READY**(blocker/major 0). minor 2건 반영: 스왑 seam 길이가드(§3.3), fail-closed 424 재시도회피(§3.5))
- 이전: rev3 — ultracode 적대검증 NEEDS_REVISION 반영: fail-closed 기전 정정, 페이지이미지 유출면, 엑셀 비범위 명시
- rev2 → rev3 변경: ①`status` 필드 fail-closed 는 소비자가 필드를 버려 무효 → **HTTP 5xx** 로 정정. ②페이지 이미지(MinIO)는 텍스트 마스킹이 못 미치는 raw PII 사본 → 별도 처리(§3.7). ③엑셀 enriched/chunks 도 unmasked → §1 보장을 **비엑셀 텍스트**로 한정.
- 이전(rev2): 마스킹 구현 제외, **배선 검증** 중심으로 재작성
- 범위: **현재 소스 기준 배선(wiring)만** 확정. doc_guard 내부 검출/마스킹 엔진은 **블랙박스**(추후 Presidio). 정규식/치환 알고리즘·PII 유형은 본 문서 비범위.
- 근거(검증한 실제 소스): `service/app.py`, `service/parse_client.py`, `parse_service/app.py`, `knowledge_base/backend/app/routers/kb.py`, `knowledge_base/backend/app/clients/kb_pipeline_client.py`, `doc_guard/app/main.py`.

## 1. 목표 (한 줄)

파서가 만든 enriched_content 를 **doc_guard 가 받아 PII 검출→마스킹(추후 Presidio)** 하고, **마스킹된 enriched_content 를 청킹**해 **비엑셀 문서의 텍스트 계층**(벡터·그래프·검색·페이지귀속 텍스트)이 마스킹본만 보게 한다. 배선: `parse → doc_guard → chunk`.

> ⚠️ 보장 범위 한정(적대검증 반영): 본 배선은 **텍스트(enriched_content) 계층**만 마스킹한다. **페이지 이미지 픽셀(§3.7)** 과 **엑셀 자체청킹 경로(§3.6)** 는 텍스트 스왑이 닿지 않는다 — 각각 별도 결정으로 다룬다. "전 구간 마스킹" 은 과장이므로 쓰지 않는다.

## 2. 검증된 현행 데이터 계약 (배선의 전제)

### 2.1 parse-svc `/parse` 반환 (`parse_service/app.py:281-309`)
```
{ enriched_content: str,
  modal_spans: [...],          # _modal_spans(enriched) — 마커 위치 기반 char_range
  page_spans: [{page_number, char_start, char_end}],   # ★ char offset
  pages: [...이미지...], page_count, docs_id, n_blocks, chunk_needed: true, timing_metrics }
```

### 2.2 facade `/parse` (`service/app.py:66-85`)
parse-svc 에 얇게 위임하고 결과를 그대로 반환. (엑셀 `chunk_needed=false` 만 `chunk_strategy` 보정.)

### 2.3 facade `/chunk` (`service/app.py:88-141`)
- `enriched_content` 를 adaptive_chunk 로 보냄.
- `atomic_markers = MODAL_ATOMIC_MARKERS` 전달 — **리터럴 `〈MODAL…〉…〈/MODAL〉` 토큰 매칭**(offset 아님) → 모달 원자성. **길이 변화에 강함**(마커 토큰만 보존되면 OK).
- `page_spans`(char_start/char_end)를 **그대로 전달**(122행) → 청크별 `chunk_pages` 귀속. **★ char offset 의존 → 길이 변화에 취약.**

### 2.4 실사용 소비 경로 (`knowledge_base` — 검증)
- kb-backend 는 facade **단계별** `/parse → /chunk → /insert` 만 씀. `/ingest` 는 **폐기**(`kb_pipeline_client.py:4-7`).
- **Phase1** `parse-preview` 가 `/parse` 응답의 `enriched_content/page_spans/pages/chunks/chunk_strategy` 를 **sidecar 로 staging 저장**.
- **Phase2** `documents/ingest` 가 sidecar 를 복원해 청킹/적재 잡에 운반(`kb.py:356-359`).
- ⇒ **마스킹은 `/parse` 응답 안에서 일어나야** sidecar·Phase2 청킹·적재까지 마스킹본이 전파된다. (Phase2 는 재파싱하지 않고 sidecar 재사용.)

## 3. 배선 결정

### 3.1 삽입 지점 = facade `/parse` 후단 (호출만)
`service/app.py:/parse` 에서 `pc.parse(...)` 직후, `parsed["enriched_content"]` 를 doc_guard 로 마스킹해 **동일 키로 교체**하고 반환. PII 로직은 facade 에 없음(HTTP 호출 1개). parse-svc 무변경.

> 왜 parse-svc 안이 아니라 facade 후단인가: parse-svc 는 파싱만 소유(단일책임). doc_guard 는 게이트/검사 전담 서비스라 PII 도 같은 소유자. facade 는 이미 얇은 오케스트레이터라 "parse→(mask)→반환" 삽입이 자연스럽고, sidecar 가 스냅샷하는 `/parse` 응답에 마스킹이 반영된다.

### 3.2 doc_guard 계약 (블랙박스 — Presidio 는 내부 교체)
- `POST /v1/mask` : 요청 `{ text: str, filename?: str }` → 응답 `{ masked_text: str, findings?: [...] }`.
- 엔진(현재 미정 → 추후 Presidio `AnalyzerEngine`+`AnonymizerEngine`)은 **이 계약 뒤에 숨는다.** facade 는 엔진을 모른다.
- **findings 는 유형·건수 집계만**(원문 PII·위치 원문 미포함 — 재노출 회피).

### 3.3 ★ 핵심 배선 불변식 — 길이 보존(length-preserving) 마스킹 **계약으로 강제**

Presidio 기본 anonymize 는 `<PHONE_NUMBER>` 라벨 치환이라 **길이가 바뀐다.** 그러면 §2.3 `page_spans`·§2.1 `modal_spans` 의 char offset 이 masked enriched_content 와 어긋나 **청크→페이지 귀속·모달 span 이 깨진다.**

→ **doc_guard `/v1/mask` 계약에 `len(masked_text) == len(text)` 를 명문화**하고, Presidio 를 **길이 보존 연산자로 구성**한다:
- Presidio `AnonymizerEngine` 의 **`mask` operator**(`masking_char`, `chars_to_mask=<엔티티 전체 길이>`, `from_end=False`) 사용 → 엔티티를 **같은 글자 수의 마스크 문자로 in-place 치환**(길이 불변). (`replace`/라벨 operator 는 **금지** — 길이 변동.)

이 계약이면:
- **facade 는 `enriched_content` 한 필드만 교체**하면 되고, `page_spans`·`modal_spans`·`pages` 는 **손대지 않아도 유효**(offset 불변). 배선이 순수 text→text swap.
- **atomic_markers(모달 원자성)** 도 자동 보존(마커 토큰 미변경 + 길이 불변).

#### ★ 스왑 지점 런타임 가드(적대검증 rev3 반영) — 계약을 seam 에서 강제

길이 보존은 **out-of-process doc_guard 계약**이라 위반돼도 facade 가 모르면 downstream 이 **조용히** 깨진다(`adaptive_chunk runner.py:776-788` text.find→-1 시 빈 chunk_pages, `:433-452` text[cs:ce] 오정렬 슬라이스). → **facade `/parse` 스왑 직전 `len(masked)==len(enriched)`(Python char length) 를 검증**하고, 불일치면 **교체하지 말고 §3.5 fail-closed(424)** 로 처리한다. 블랙박스 엔진을 신뢰하지 않고 **배선 seam 에서 linchpin 불변식을 강제**(§6 의 doc_guard 단위테스트와 별개의 런타임 가드).

> 대안(길이 가변 라벨 치환을 꼭 원할 경우): doc_guard 가 `masked_text` + **엔티티 offset/Δ맵**을 반환하고, facade 가 `page_spans`·`modal_spans` 를 **재매핑**해야 함(모달 재스캔 로직 필요, facade 비대화). → **비채택**(배선 복잡·불변식 위험). rev2 는 길이 보존 계약으로 확정.

### 3.4 doc_guard 가 지켜야 할 보존 계약 (마스킹 대상 제외구간)
- **모달 마커 `〈…〉`(U+3008/U+3009) 토큰 byte-identical** — 마스킹이 마커 괄호/토큰을 매칭·치환 금지.
- **`<table>` HTML 태그·속성 보존** — 셀 **값** 내부 PII 만 마스킹, 태그는 불변.
- (구현은 doc_guard 소유. 배선 관점에선 "이 두 보존 + 길이 보존" 이 **인수 계약**.)

### 3.5 실패 처리 = fail-closed (기본) — ★ HTTP 5xx 로 정정(적대검증 ①)

**주의**: `{status:"failed"}` 200 바디는 **작동하지 않는다.** 소비자 `KbPipelineClient.parse`(`kb_pipeline_client.py:161-172`)가 응답을 명시 키로 재구성하며 `status`/`detail` 을 **버리고**, 유일한 실패 감지는 빈 컨텐츠 가드(`tasks.py:459`, `pipeline.py:2027`, `if not enriched.strip() and not chunks`)뿐이다. 순진하게 `parsed["status"]="failed"; return parsed` 하면 raw enriched 가 남아 **fail-OPEN 유출**된다.

→ **doc_guard 실패 시 facade `/parse` 는 비재시도 4xx `424 Failed Dependency` 를 던진다.** 그러면 클라이언트 체인의 `raise_for_status()`(`parse_client.py:41` → `kb_pipeline_client.py:159` → Phase1 task try/except)가 걸려 잡이 실패 처리된다. (보강: 4xx 경로에서 raw enriched/chunks 를 절대 바디에 싣지 않는다.)
- **왜 5xx 가 아니라 424 인가(적대검증 rev3 반영)**: 클라이언트 `_request` 는 `status_code >= 500` 을 **최대 3회 재시도**(`kb_pipeline_client.py:115-126`, max_retries=3)한다. doc_guard 지속 장애 시 5xx 면 **~400s+ 파싱(`pc.parse`)을 3× 반복**하는 낭비가 생긴다. 424 는 `>=500` 재시도 필터를 벗어나면서도 non-2xx 라 `raise_for_status`(`:159`)를 트립 → **재파싱 없이 즉시 실패**. (fail-closed 정확성은 5xx/424 동일 — 잡 FAILED, raw 미저장.)
- 완화 스위치 `KBP_PII_FAIL_OPEN=false`(기본 off). true 일 때만 마스킹 실패 시 원문 통과(경고 로그) — 운영 기본 off.
- 인수 조건: 실패 응답에 **raw enriched_content/chunks 잔존 0** (§6 체크).

### 3.6 적용 조건 / 범위 — 엑셀 비범위 명시(적대검증 ③)
- `chunk_needed=true`(비엑셀)·enriched_content 비어있지 않을 때만 마스킹.
- **엑셀(`chunk_needed=false`)은 v1 비범위.** 단, 엑셀 게이트(`doc_guard.check_excel`, `tasks.py:420-438`)는 **차단만** 하지 마스킹하지 않으므로, 엑셀 `enriched_content`(`parse_service/app.py:232-233`)는 sidecar→브라우저 프리뷰로, raw `chunks[]`(`:236`)는 pre_parsed→insert 로 **unmasked 도달**한다. → §1 보장을 **비엑셀**로 한정(위 경고 박스). 추후 엑셀 PII 를 원하면 엑셀 enriched_content + chunk 텍스트(둘 다 평문 str)도 doc_guard mask 를 경유시키면 됨(별건).

### 3.7 ★ 페이지 이미지 계층 — 텍스트 마스킹이 못 닿는 유출면(적대검증 ②)

parse-svc 는 `/parse` 안에서 **원본 바이트로 페이지 이미지를 렌더→MinIO 업로드**(`parse_service/app.py` `_render_and_upload`→`put_page_image`)하며, 이는 **facade 텍스트 마스킹보다 먼저** 일어난다. 이미지는 청크별 `chunks_meta.minio_image_object`(`pipeline.py ~759`, `documents.py:66`)로 문서상세에 노출된다. **enriched_content 문자열 스왑은 이미지 픽셀을 절대 못 건드린다** → 시각 PII 유출.

**결정(확정) = (B) 시각 PII 는 v1 비범위.** 페이지 이미지는 **원본 그대로 MinIO 에 적재**하고, 마스킹은 **텍스트(enriched_content) 계층만** 담당한다. §1 보장은 "텍스트 계층"으로 한정(반영 완료). parse-svc 는 **무변경**(이미지 렌더/업로드 로직 손대지 않음). 이미지 상 PII redaction 은 후속 별건(OCR 후 마스킹 또는 이미지 블러).
- 근거: 사용자 결정 — "텍스트만 마스킹, 이미지는 그대로 minio 적재".
- 기각: (A) 마스킹 ON 시 이미지 억제(썸네일 상실 + parse-svc 무변경 원칙 위배), (C) 이미지 redaction(고비용, v1 밖).

## 4. 배선 흐름 (확정)

```
kb-backend Phase1  ──POST /parse──▶  facade /parse
                                       parse-svc /parse → {enriched_content, page_spans, modal_spans, ...}
                                       ├─ enriched 비어있음/실패 → 그대로 반환(마스킹 skip)
                                       └─ doc_guard POST /v1/mask {text=enriched}
                                             → {masked_text}  (len 불변 계약)
                                          parsed["enriched_content"] = masked_text   # 한 필드만 교체
                                          (page_spans·modal_spans·pages 불변 → 유효)
                                       반환(masked enriched_content 포함)
                     sidecar 저장(masked enriched + 유효 offset)
kb-backend Phase2  ──POST /chunk (masked enriched + page_spans)──▶ adaptive_chunk (마스킹본 청킹)
                   ──POST /insert──▶ edgequake (마스킹본만 적재)
```

## 5. 변경 지점 (배선만 — 구현 디테일 제외)

| 대상 | 배선 변경 | 비고 |
|------|-----------|------|
| `doc_guard/app/main.py` | `POST /v1/mask` 엔드포인트 추가(계약 §3.2/§3.3/§3.4) | 엔진 내부는 추후 Presidio |
| `service/doc_guard_client.py` (신규) | facade→doc_guard httpx 래퍼(`ParseSvcClient` 패턴) | `mask(text)->masked_text` |
| `service/app.py` `/parse` | `pc.parse` 후단에 mask 호출 + enriched 교체 + fail-closed | `get_doc_guard` 팩토리, `dg=Depends(...)` |
| `scripts/facade.env` | `KBP_DOCGUARD_URL`(기본 `http://localhost:8000`, compose `http://doc_guard:8000`), `KBP_PII_FAIL_OPEN=false` | |
| parse-svc | **무변경** | 파싱만 소유 |
| (선택·별건) `service/app.py` `/ingest` 제거 | 죽은 경로 정리 — 배선 필수는 아님 | 소비자 폐기 확인됨 |

## 6. 배선 검증 체크리스트 (완료 조건)

- [ ] doc_guard `/v1/mask` 가 **len(masked)==len(input)** 을 지킴(계약 테스트) — page_spans/modal_spans offset 유효.
- [ ] facade `/parse` 스왑 seam 에 **런타임 길이가드**(`len(masked)==len(enriched)`, 불일치→424 fail-closed) — 계약을 코드로 강제(§3.3).
- [ ] fail-closed 는 **424**(비재시도) — 지속 장애 시 ~400s 파싱 3× 반복 안 함(§3.5).
- [ ] `〈MODAL〉` 토큰·`<table>` 태그 byte 보존(계약 테스트).
- [ ] facade `/parse` 가 enriched **한 필드만** 교체하고 page_spans/modal_spans/pages 를 손대지 않음.
- [ ] doc_guard 다운 시 `/parse` → **HTTP 5xx**(fail-closed), 클라이언트 `raise_for_status` 로 잡 실패. 실패 응답에 **raw enriched/chunks 잔존 0**.
- [ ] Phase2 sidecar 재사용 경로에 마스킹본이 전파됨(재파싱 없음 확인).
- [ ] 엑셀(`chunk_needed=false`)은 마스킹 skip **이며 §1 보장 밖**(엑셀 enriched/chunks unmasked 문서화).
- [ ] **페이지 이미지 계층(§3.7)** = (B) 확정 — 이미지 원본 MinIO 적재, parse-svc 무변경, §1 보장 텍스트 한정 문구 일관.

## 7. 유보 (배선 밖 — 추후 결정)

- 검출/마스킹 엔진 = **Presidio**(AnalyzerEngine 인식기 목록·언어 ko/en·`mask` operator 파라미터). 본 배선 계약(§3.2~3.4)만 지키면 언제든 붙임.
- 마스크 문자(`●` 등)·PII 유형 목록·오탐 튜닝.
