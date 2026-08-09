# 02 · 변경내용

> 출처: SoT.md §0/§11, SoT.v1.md, dev_wiki.md, process-definition 개정이력.

---

## 0-C. 표(table) 원자 보존 — 〈MODAL〉 wrap + page 독립 경쟁 경로 (4레포, 2026-07-27~28)

**문제**: 대형 문서에서 청커가 `<table>` HTML 을 청크 경계에서 `<td` 중간에 쪼개 렌더 깨짐 + 검색 시 행 손실. 실측(소장 46p): table 포함 청크의 **55%(22/40)가 쪼개짐**. test_doc(신탁 3p, 3/8)보다 훨씬 심각. 가중치 튜닝(bi.40)으로는 불가 — 모든 청킹 방법이 대량 쪼갬.

**해법(MODAL wrap, LLM 0회)**: 파서가 table 을 `〈MODAL type="table"〉[앞문맥 ≤200자]<table>…</table>[뒤문맥 ≤100자]〈/MODAL〉` 원자 마커로 감싼다(U+3008/U+3009). 청커가 `MODAL_ATOMIC_MARKERS` 로 이 스팬을 통째 보존. **실측: 소장 table 쪼갬 22→0**(recursive_1100 = 86청크, split 0). LLM summary 없이도 마커 유효 → 비용 0.
- **wrap_modals 분리**: 기존엔 마커 wrap 이 LLM enrich(`KBP_MODAL_ENRICH`)에 하드 커플. 신규 env `KBP_MODAL_WRAP`(기본 "1"=on)로 분리 — wrap 은 항상, LLM summary 는 별도. (`kb_pipeline/modal.py` `_enrich_core`/`enrich`/`enrich_with_spans` 에 `wrap_modals` 스레딩, `_assemble` per-decision `bare` 플래그.)
- **문맥 복사(LLM 0) — 2026-08-02 계약 변경(흡수→복사)**: 초기엔 휴리스틱(≤40자·`#`끝·`단위/서식/별표/제N`·항목번호 / `※·*·주)·[단위·(단위` prefix)으로 제목·각주를 판정해 **흡수(이동)** 했으나, 페이지 오귀속(다른 페이지 블록이 표 페이지로 재귀속)·인접표 선점·무관블록 유실 때문에 폐기. 현재는 **패턴 판정 없이 글자수 규칙으로 복사**한다 — 앞 블록 **끝 200자**(`_CTX_COPY_BEFORE_CHARS`) + 표 + 뒤 블록 **앞 100자**(`_CTX_COPY_AFTER_CHARS`), 윈도우 내 **첫 비공백 블록 1개**(`_first_nonblank`). **원본 블록은 그대로 남는다**(tc=fc=0 → consumed 공집합) → 오귀속·유실 구조적 불가. 대가는 최대 300자 중복(임베딩·edgequake 엔티티 2회 계상)과 고아 마이크로청크. `decisions[i]["ctx_mode"]` 명시 플래그로 `_assemble` 이 복사/흡수 경로를 **배타 분기**. **LLM 경로(`KBP_MODAL_ENRICH=1`)는 흡수 그대로 불변.** 삭제: `_heuristic_title`/`_heuristic_footnote`/관련 정규식/`import re`.
- **oversize 가드(2단계)**: 조립 span >~13800자(≈6000토큰, bge-m3 8192 마진)면 ①먼저 **복사 문맥(ctx)만 버려** 마커 원자화는 유지하고, ②본체(요약+payload+흡수분)만으로도 초과할 때만 `bare=True` → 마커 없이 emit(쪼갬 허용). 1단계가 없으면 13500자 표가 문맥 300자 때문에 원자성을 잃는다. 다페이지 대형표 ContextWindowExceeded 방지.
- **마커 스트립(백엔드 2지점, 프론트 무수정)**: facade `service/edgequake.py insert_chunks`(적재 공통싱크 — /insert·/ingest 커버) + `service/app.py /chunk 응답`(chunks_meta 표시사본 커버). `_strip_modal` = `〈MODAL…〉`·`〈/MODAL〉` 만 제거, 내부(제목+표+각주) 보존. 청킹 입력(`text=`)은 마커 유지. **실측: 저장/표시 청크 마커 0**.

**후속 버그 발견·최종 수정 — PageSplitter × marker-aware 비호환**: MODAL wrap 이 활성화한 기존 잠복 버그. marker-aware 경로(`service/runner.py`)가 gap 마다 선택 splitter 를 실행하는데, `PageSplitter.split(doc)` 은 `doc`(gap)을 무시하고 `self.parsed.pages`(전체 문서 페이지)를 반환 → **gap 수 × 전체 페이지 중복 → 청크 폭증(소장 26마커→1832청크)**. 2026-07-27 임시조치로 atomic 문서에서 page를 경쟁 제외했으나, 페이지가 가장 자연스러운 문서도 page 후보를 잃는 과잉 차단이었다. **2026-07-28 최종 정책**: `page`는 MODAL과 무관하게 전체 원문의 페이지를 딱 한 번 분할(마커가 페이지 경계를 지나면 깨져도 page 우선), 그 외 방법은 gap 청킹+MODAL 원자 조립. 각 방법의 **실제 최종 반환 청크**를 동일한 원문/BI/coref 기준으로 한 번에 채점한다. page 직접 선택(`skip_scoring`)도 동일하게 전체 페이지 1회만 실행한다. 검증: page 경쟁 복귀, page 승리 시 원본 2페이지=2청크, non-page 승리 시 MODAL 1원자, PageSplitter 1회, coref prime 1회, skip page 중복 0 + 전체 pytest green. 라이브 :18060 auto probe에서 `recursive_600`·`page` 모두 `methods_compared` 합류, page 직접 선택에서 경계횡단 MODAL을 원본 2페이지로 반환.

**부수 수정**: 앞선 정식 BI 배선(0-D 참조) 잔재 — `service/tests/test_chunk_endpoint.py` 의 `FakeAdaptiveChunk.chunk()` 에 `blocks` kwarg 누락으로 5개 red → `blocks=None` 추가.

**후속 개선 — overlap 에서 MODAL 원자 청크 제외(adaptive_chunk, 2026-07-27)**: `apply_overlap` 이 표 원자성 위해 꼬리를 표 시작까지 확장(`_expand_start_out_of_table`) → 원자 table 청크의 꼬리=표 전체 → 다음 청크에 표 통째 복제(실측 소장: 원본 26 → 청크 `<table>` 등장 **47**, 복제 21). 임베딩/검색 인덱스 중복 낭비. **수정**: `Chunk.atomic` 플래그(default False, `_chunk_to_dict` 미노출 → API 불변) → runner 원자 세그먼트 조립(marker-aware+skip_scoring 양경로) `atomic=True` → `apply_overlap` 이 `cur.atomic or prev.atomic` 경계면 prepend 스킵. 비원자↔비원자 overlap 은 불변(산문 검색문맥 보존). cur.atomic 스킵은 트레이드오프 아닌 **기존 원자성 계약 회복**(production 기본 overlap=100 에서 "MODAL청크=표1개"가 이미 깨져있었음). 검증: 유닛 141 pass, 소장 skip_scoring E2E overlap=100 `<table>` 47→**26**(복제 0). bare/미종결마커 표는 범위 밖(pre-existing). opus5 3라운드 검증 READY. 커밋 `81fce5b`.

**후속 개선 — 표 주변 문맥을 MODAL 안으로(2026-08-02, 커밋 `3edc40c`·`3ff9ab0`)**: 표 제목/각주가 다른 청크로 흩어져 검색 시 표만 회수되던 문제. 파서(`kb_pipeline/modal.py`)가 표 **앞뒤 각 200자**를 MODAL 스팬 안에 넣는다(LLM 0회).
- **경계 규칙**: 글자수(200)가 최우선, **페이지 경계는 조건이 아니다**(표가 페이지 최상단이면 이전 페이지까지 긁음 — 앞은 복사라 페이지 오귀속 불가). 제목(`text_level`)을 만나면 앞은 **포함**하고 중단, 뒤는 **제외**하고 중단(뒤 제목은 다음 섹션 것). 앞쪽은 **연속 제목 뭉치를 통째로** 가져온다 — 실측(휴가규정) 표 직전 `(개정 2025.09.01.)` 에 text_level 4 가 붙어 첫 제목에서 멈추면 진짜 제목(level 3)을 놓쳤다.
- **이동 vs 복사**: 각주 표기(`주)`/`※`/`*`/`주1)`)로 시작하는 뒤쪽 블록만 **이동**(consume), 나머지는 **복사**(원본 보존). 무조건 이동 시 실측(KIS) 9건 중 3건이 각주가 아닌 본문·제목이라 원래 자리에서 사라졌다.
- **문장 경계**: 200자 컷이 문장 중간이면 그 문장 처음까지 확장(총 400자 한도, 넘으면 다음 문장부터로 축소). 오탐 차단 — 소수점(`18.9%`)·날짜(`2025.09.01.`)는 경계 아님. 뒤쪽은 예산 초과 **각주 블록**만 온전한 문장까지 발췌하고 `movable=False` 강제(부분 발췌 이동은 나머지 유실). 각주 아닌 초과 블록까지 살리면 다음 절 본문이 유입됐다(KIS `## Key Issue Update …`).
- **A-guard 불변**: 조립 추정치가 `_OVERSIZE_CHARS`(13800) 초과면 ①문맥 포기(원자성 유지) → ②본체만으로도 초과면 bare 강등.
- **실측**: 휴가규정 BEFORE=`가정의례와 관련된 청원휴가 허가기준 / (개정 2025.09.01.)`, AFTER=각주 3줄 이동. KIS 19모달 — 어절 중간 시작 0건, 비어있지 않은 AFTER 전부 진짜 각주. 유닛 225 pass.
- **리스트 기호 복원(2026-08-02, 커밋 `615b2cd`)**: markdown-it 이 `- `/`* `/`1. ` 를 구조로 소비해 텍스트에서 지운다. ODL 이 각주를 `- * 각 대상에…` 로 내보내면 **중첩 리스트**로 파싱돼 `-`·`*` 가 둘 다 소실되는데, 옆줄 `- ** 사망…` 은 `**` 가 불릿이 아니라 살아남아 **같은 각주 3줄이 서로 다르게 처리**됐다(하나만 복사 → 표 밖 중복). `blockify` 가 항목 텍스트에 기호를 복원하고 `list_markers` 로 무엇을 복원했는지 남긴다. `modal._looks_like_footnote(text, is_list_item)` 는 **구조 기호만** 벗기고 `*` 는 남긴다(각주 표기이므로). 번호 항목(`1) …`)은 `list_markers` 가 있을 때만 벗겨 복원된 조문이 각주로 오인돼 표 안으로 끌려가는 회귀를 막는다. **레인 영향**: odl·paddle_gw 는 `hybrid_to_blocks` 경유라 적용, **vl 레인은 `elements_to_blocks` 가 `_extract_content` 값을 그대로 담아 미적용**(원래도 마커가 보존돼 있었음 — 이 변경은 레인 간 표현을 맞추는 방향). 실측: 휴가규정 각주 3줄 전부 1회(이전 첫 줄만 2회), 신탁지침 MODAL 8쌍 문자 단위 동일.
- **두 표 사이 라벨 제외(2026-08-02, 커밋 `8356055`)**: `<표T4> 서식1# <표T5> (단위:백만원) <표T6> …` 처럼 표 사이에 낀 라벨이 **앞 표의 뒤 문맥으로도 복사**돼 문서에 3회씩 등장했다(원본 + 앞 표 AFTER + 다음 표 BEFORE). `_next_is_modal(blocks, idx)` — 다음 블록(빈 블록 건너뛰고)이 모달이면 뒤 문맥에서 **제외하고 중단**. 제목 경계로도 막히지만 스캔 레인은 헤딩이 0개라 무력해서, **구조만 보는 규칙**으로 만들어 `is_heading` 판정보다 앞에 뒀다. 기각한 대안: "'서식' 포함 → 헤딩"은 실측 8건 중 5건이 본문 문단 오탐(63%), "20자 이하 단독 블록"은 휴가규정 조문 22건 오탐. 실측: 서식1# 6→5, 서식1-1# 4→3, 서식2# 5→4, (단위:백만원) 4→3. 다음이 본문인 각주(`주1) …`)는 그대로 이동(회귀 0).
- **두 모달 사이 ≤50자 구간 흡수(2026-08-02, 커밋 `9acbb69`)**: 청크가 MODAL 경계로 잘리므로 표와 표 사이 짧은 텍스트는 파편 청크가 된다. 규칙은 **"각주면 앞 모달, 아니면 뒷 모달"** — (1) 다음이 표여도 **각주 표기 블록은 앞 모달**이 가져간다(`8356055` 의 '다음이 표면 무조건 제외'를 각주 예외로 완화), (2) 앞 모달이 각주를 가져간 뒤 남은 구간이 `_CTX_ABSORB_GAP_CHARS`(50) 이하면 **뒤 모달로 이동**(원본 소멸), 초과면 종전 복사. **임계 근거**(실측 26구간): `0자×8 / 4~10자×7 / 196 / 322 / 433 … 3413` — 라벨(≤10자)과 본문 서술(≥196자) 사이가 비어 cap 100/200/300 결과가 동일하다. 본문까지 흡수하면 청커가 못 쪼개는 수천 자 원자 청크 + A-guard 가 문맥을 통째로 버릴 위험. **안전장치 3**: 복사 경로 전용(LLM 경로에 적용 시 `ctx_mode` 가 켜져 byte-identical 회귀), 다른 페이지 블록 이동 금지(page_spans 소실), A-guard 가 문맥 버린 모달 제외(`ctx_dropped`). 실측: 휴가규정 구간 0·0·0자, 신탁지침 `[433,10,769,4,8,6,4]`→`[433,0,769,0,0,0,0]`, KIS 무변화(≤50자 구간 0개).
- **스캔 레인(paddle_gw) 실측(신탁업무처리지침 3p)**: markdown 헤딩 0개 → `text_level` **전무**. MODAL 8개 전부 `type="table"`(이미지 0 — `_strip_gateway_image_refs` 가 블록화 전에 `imgs/` 참조를 제거, 수식 0). 제목 경계가 없으니 앞뒤 모두 200자 예산으로만 잘리는데, 각주 패턴 블록이 하나도 없어 **이동 0건 = 전부 복사** → 유실은 없다. 다만 인접표 사이 라벨이 양쪽에 붙는다(`서식1#` 이 T4 뒤·T5 앞, `(단위:백만원)` 이 T5 뒤·T6 앞) — 노이즈이지 손실 아님.

**검증**: 파싱 단위 소장 enriched 마커 26=table 26=modal_spans 26. recursive_1100 = 86청크 쪼갬 0. facade `/chunk` 응답 마커 0. 유닛 parse-svc/facade 83 pass + adaptive 117 pass. **비고**: 큰 문서 auto 청킹은 스코어링(coref/임베딩) 때문에 수분 소요(정상, job_timeout 3600). **미완**: full end-to-end 적재→검색→렌더 원샷 재확인(세션 전환으로 서비스 다운, 조각별로는 검증됨), 4레포 커밋.

## 0-B. 그룹 기반 KB 접근제어 + Postgres 통합 (설계 확정·계획, 2026-07-04)

**요구**: KB 생성 시 **그룹 하나**를 지정하고, 그 그룹 멤버만 해당 KB 를 읽기/검색. 그룹엔 유저 N명(계속 추가 가능),
모든 유저가 빠진 그룹은 삭제되며 그러면 그 KB 는 검색 불가여야 함. 계획: `docs/superpowers/plans/2026-07-04-group-based-kb-access-control.md`
(v4 READY — codex 백엔드 hang 으로 adversarial 대체검증 3라운드: v1 13건→v2 3건→v3 1건, 전부 해소).

- **핵심 결정: 접근제어는 파이프라인이 아니라 kb-backend 인가계층.** facade/parse/chunk/edgequake **코드 무변경**(예외: 런처 + facade 시크릿게이트).
  - **`tenant_id` 에 그룹 매핑 금지** — edgequake 는 이미 workspace-per-KB(`kb-<kbid>`)로 물리격리하므로, **KB↔group 이 1:1 이면 workspace 가 곧 그룹격리**. tenant_id 를 그룹으로 재정의하면 중복·경직(그룹 이관 시 청크·그래프 재태깅)·의미충돌.
  - **데이터모델(kb-backend)**: `groups`, `group_members`(user↔group **M:N**), `knowledge_bases.group_id` FK(KB↔group **1:1**). 유저는 여러 그룹 소속 가능.
  - **판정**: `acl.can_read_kb` = KB group 멤버십 **만**(owner 라는 사실만으로는 read 권한 없음 = "대체"; owner 는 관리권한만). 개별 `kb_shares`(user↔user 공유) **은퇴**(410 Gone, 테이블은 dormant 유지).
  - **그룹 삭제→검색불가**: `knowledge_bases.group_id` FK `ON DELETE SET NULL` + `can_read_kb` 의 `group_id IS NULL→False`. 그룹 지우면 KB 가 orphan(검색불가), edgequake 행 재태깅 0. (RESTRICT 는 삭제를 막아버려 요구와 모순 → 기각.)
  - **KB 생성 시 기존 그룹 필수 매핑(2026-07-07 확정)**: `POST /kb` 의 `group_id` 는 **필수**. **그룹을 먼저 만든 뒤** 그 group_id 로 KB 를 생성한다(존재검증 → 없으면 422, 미지정 → 422). 초기 구현의 "선택 입력 + 미지정 시 소유자 개인 기본그룹 자동생성" 은 **폐기**(자동 기본그룹·미매핑 NULL 생성 모두 금지). 생성 후 그룹 이동/해제는 `/admin/groups/[groupId]` GroupKbsPanel(`PUT/DELETE /groups/{id}/kbs/{kbId}` = `SqlKbRepo.set_group`)에서 수행하며, 그룹 삭제 시엔 FK `SET NULL` 로 미매핑 orphan 이 될 수 있다(검색 불가). ※ 기존 KB 를 기본그룹으로 넣은 **마이그레이션 백필은 유지**. 필수화로 group_id 없이 POST /kb 하던 테스트들은 conftest `_make_autogrant_client`(그룹 auto-provision + owner grant)로 전제를 제공한다. 대응 plan `2026-07-04-group-based-kb-access-control.md` 상단 CLARIFIED 배너 참조.
  - **경계 강화**: "검색 파라미터는 보안경계가 아님" — facade(:19000)는 인증없이 workspace_id 를 클라가 넘김. 그룹판정은 인증주체 **kb-backend** 가 자기 DB 조회로 수행하고, **facade 를 `X-Facade-Key` 공유시크릿으로 잠가** 우회(직접호출) 차단. kb-backend 만 facade 호출.
  - **해석 흐름**: 프론트→(JWT)→kb-backend 가 user.groups 도출→허용 KB(workspace_id) 집합 계산(`group_ids_for_user`→`list_by_group_ids`)→그 workspace 만 facade 로 질의(기존 스코핑 재사용).
- **Postgres 통합(운영 목적)**: 두 서버(edgequake :5433/`edgequake` vs kb-backend :5432/`kb_orchestrator`)를 **한 인스턴스·두 database** 로. 확장(pgvector·AGE)은 DB단위라 edgequake DB 에만. ⚠️ 런처 `start_dedicated_edgequake.sh` 가 매 기동 `docker rm -f`(볼륨없음)로 **PG 소거** → 통합 전제조건으로 **영속 named volume + 멱등 기동 + kb 롤/kb_orchestrator DB 부트스트랩** 으로 개조. kb-backend DSN(config 기본값 포함) → `:5433/kb_orchestrator`.
- **강제수준**: 앱레벨 ACL + facade 잠금. **RLS 하드닝(edgequake non-superuser 롤 + FORCE RLS)은 후순위 W4 로 분리**(그룹기능과 직교, 본 계획 비범위).
- **상태**: 계획 v4 READY, **구현 미착수**. 착수 시 phase 진행마다 `03-dev-progress.md` 중간반영.

---

## 0-A. 문서단위 그래프(관계) 추출 스킵 — UI 라디오 + metadata 게이트 (3레포, 2026-06-30)

엑셀처럼 그래프 추출이 무의미·고비용(청크 2천급)인 문서를 **벡터 적재/검색만** 하고
edgequake 엔티티/관계 추출(qwen 동기 블로킹 단계)을 **문서 단위로 건너뛴다**. provider=kb_pipeline 전용.
계획: `docs/superpowers/plans/2026-06-30-excel-skip-graph-extraction.md` (v3 READY, ultracode 적대적검증 2회).

- **신호 흐름**: UI 라디오(extractGraph) → kb `/documents/ingest`(`extract_graph`) → facade `/insert`(`extract_graph`)
  → edgequake `metadata.skip_graph_extraction`. **엑셀(chunk_strategy=excel_rag_parser)은 UI에서 '미추출' 고정(disabled)**.
- **edgequake(Rust)**:
  - (선행 버그수정) async 업로드 경로가 `request.metadata` 를 버리던 것 수정 — `text_upload.rs` 에서
    base object에 merge(보호키 document_id/title/tenant_id/workspace_id 우선, 그 외 사용자키 추가). 이 수정 없이는 skip 신호 미도달.
  - `processing.rs` 에 `process_with_resilience_cancellable_opts(skip_extraction)` 신규 — 추출 서브블록만
    `&& !skip_extraction` 게이트. `chunk_async`(청킹)·`finish_document_processing`(청크임베딩·lineage)는 게이트 밖이라 유지 → **벡터검색 무영향**. 기존 메서드는 false 위임이라 다른 호출자 무영향.
  - `text_insert.rs`: `skip_graph_extraction`(bool) 읽어 opts 호출. **status 가드** — skip 시 `entity_count==0`을
    `partial_failure` 아닌 `completed` 로(안 고치면 facade `document_phase` 가 `_PHASE_SUCCESS` 제외→/insert가 failed 반환). 진짜 LLM실패(skip=false) 0엔티티는 여전히 partial_failure.
- **facade**: `/insert` 에 `extract_graph: bool=Body(True)` 추가 → `insert_chunks(skip_graph=not extract_graph)`
  → **`submit_document`**(post_document 아님) 본문에 `skip_graph` True일 때만 `metadata.skip_graph_extraction=true` 첨부(기본은 byte-identical).
- **knowledge_base**: UploadPanel 라디오(삼항 바깥 공통 위치, 엑셀 disabled+false 고정), api.ts/스키마/라우터/워커/`KbContext`/
  `_ingest_kb_pipeline_tail`/`kb_pipeline_client` 에 `extract_graph` 배선(+`KbPipelineLike` Protocol). **provider==kb_pipeline 분기 한정**(타 provider None→미첨부, 격리).
- **검증**: edgequake `cargo build` PASS + pipeline lib 215 tests PASS; facade pytest 52 PASS;
  kb tsc PASS + 대상 38 tests PASS(broader 6 실패는 dify/raganything/ragflow gate 관련 **pre-existing**, 본 변경과 무관 확인). 교차계약 일관성 검증 consistent.
- **부작용/이득**: skip 문서는 그래프·커뮤니티/글로벌검색에서 빠짐(의도), 벡터검색은 정상. qwen 추출 생략으로 엑셀 적재 시간·비용 대폭 절감. 같은 KB 혼합 안전.
- **비범위**: 이미 그래프 생성된 문서의 사후 skip 전환(checkpoint-resume가 게이트 우회), 라이브 E2E 스모크.
- **배포순서**: edgequake(재빌드+재기동, `EDGEQUAKE_CHUNKER=passthrough` 유지) → facade(재기동) → knowledge_base(백엔드+프론트). 하위호환(기본 추출 ON)이라 부분배포 중 회귀 0.

---

## 0. Chunk method 선택 passthrough (facade B, 2026-06-29)

knowledge_base plan `23_plan_chunk_method_selection.md` §B 반영. adaptive_chunk(:18060)가
`options.methods`/`skip_scoring`/`llm_regex_pattern`(A 확정 계약) 을 받게 되어, facade 가
이를 **통과만** 시킨다(검증/의미는 adaptive_chunk 소유).

- `service/adaptive_chunk.py AdaptiveChunkClient.chunk()` — kwargs `methods=None`/
  `skip_scoring=False`/`llm_regex_pattern=None` 추가. 비-기본일 때만 `POST /chunk/jobs`
  의 `options` 에 실음. 세 값 모두 기본이면 `options={"atomic_markers":…}` 로 **byte-동일**
  (auto 경로 회귀 0, 하위호환).
- `service/app.py POST /chunk` — Body 필드 `methods`/`skip_scoring`/`llm_regex_pattern`
  (전부 optional, embed) 추가 → `ac.chunk(...)` 전달. 무지정 호출 = auto(현 동작 불변).
- `POST /parse` 변경 없음(이미 `enriched_content` 반환).
- 라이브 probe(:18060): `recursive_600`+`skip_scoring` → `method_selected=recursive_600`,
  `methods_compared=[]`, `scores={}`. `llm_regex`+`제\d+조` → 조문별 4청크(LLM 패턴생성 생략).
- 테스트: `service/tests/test_chunk_endpoint.py` +3 (FakeAdaptiveChunk·auto 회귀가드 갱신).
  full suite 141 passed (기존 138→141, 회귀 0; 무관한 pre-existing 1건
  `test_insert_endpoint` stale assert 제외).
- **C(KB 백엔드) 미구현**: 이 facade 입력 계약을 KB 백엔드가 `KbPipelineClient.chunk()`
  로 호출(plan §C3 예정).

---

## 1. v1 → v2 결정 변천 (해소된 모순)

`SoT.v1.md`(원본)은 방향성 메모였다. 4개 참고 레포(`excel-parser-markitdown`, `adaptive_chunk`, `raganything_svc`, edgequake 본체)를 코드/로그 레벨로 검증한 뒤 모순을 제거하고 "차용 vs 신규"를 확정한 것이 v2(SoT) 다.

| v1 의 문제 | v2 의 해소 |
|------------|------------|
| "adaptive_chunk → edgequake insert" 정면충돌(edgequake 내부 청킹) | edgequake 공개 `ChunkingStrategy` trait 에 꽂아 해소 → 이후 facade 소유로 재확정 |
| "edgequake 스키마 차용" vs "edgequake 에 insert" 혼재 | edgequake 를 **엔진으로 운용**(베이스), 앞단만 커스텀으로 확정 |
| per-KB "schema/RLS" 표현 모호 | edgequake 가 **실제 Postgres RLS 보유**(009/013/022) → 공유테이블+tenant/workspace RLS 로 확정 |
| 파서 분담이 실측과 불일치 | 확장자별 파서 **실측 재확정** |
| raganything 교체 여부 미결 | raganything 는 엔진이 아니라 "모달 LLM 서술" 아이디어만 차용 |
| 그래프 이중생성 위험 | content 를 **단일 스트림**으로 → edgequake 가 추출/그래프 단독 소유 |

**v1 의 미해결 질문**(SoT.v1.md): "raganything 의 content_list 방식이 더 효율적이지 않나?" → **답: 엔진 채택 아님.** raganything `modalprocessors` 의 모달 서술 *구조만* 차용하고, content 는 단일 enriched 스트림으로 만들어 edgequake 추출에 일임(이중생성 제거).

---

## 2. 청킹 소유권 이전 (v1 edgequake-adaptive → v2 facade-passthrough)

가장 큰 아키텍처 변경. **2026-06-24 정정.**

### v1 경로 (구버전, 더 이상 사용 안 함)
- edgequake 내부에 `AdaptiveChunkStrategy`(Rust) 구현: `〈MODAL〉` 경계 분리 → 모달은 단독 atomic 청크, 텍스트 갭만 adaptive_chunk `/chunk` HTTP 위임. `Pipeline::with_chunking_strategy()` 신설 + 팩토리 플래그 `EDGEQUAKE_CHUNKER=adaptive`.
- 당시엔 "adaptive_chunk 의 어느 엔드포인트도 모달 스팬 원자성을 강제하지 않음(codex 확인)" → 원자성을 전적으로 Rust 전략이 소유했다.

### v2 경로 (현행)
- **청킹·모달원자성을 facade `/chunk` 가 소유.** facade 가 adaptive_chunk 를 `atomic_markers=〈MODAL〉…〈/MODAL〉` 로 호출 → adaptive_chunk `service/runner.py` `_segment_atomic`/`DEFAULT_ATOMIC_MARKERS` 가 모달 원자성을 강제. → 구버전의 "atomic 미강제" 메모는 **무효화**됨.
- 전용 edgequake 는 **`EDGEQUAKE_CHUNKER=passthrough`** 로 띄워 facade 청크를 U+001E 경계로 그대로 저장(재청킹 금지).
- ⚠️ `adaptive` 로 띄우면 facade 가 이미 청킹한 내용을 다시 adaptive_chunk 로 재청킹(이중청킹)하다 빈 구분자 조각(``)을 보내 **HTTP 422 → 적재 실패**.
- 기동 스크립트 `service/scripts/start_dedicated_edgequake.sh` passthrough 로 갱신(8.kb-pipeline `456d52a`).

---

## 3. edgequake fork 버그 수정 3건

런타임 중 발견·수정해 fork 에 반영. (실 서버 바이너리 = 루트 패키지 `edgequake`(`cargo build --bin edgequake`), `edgequake-api`(lib) 아님.)

### 3.1 임베딩 차원 미적용
- `OpenAIProvider` 가 미지 모델을 **1536 하드코딩** + `provider_setup` 이 `EDGEQUAKE_EMBEDDING_DIMENSION` 을 로깅만 함.
- 수정: `with_embedding_dimension` 추가 + 적용해 `EDGEQUAKE_EMBEDDING_DIMENSION=1024` 반영.
- `edgequake-llm` 이 crates.io 의존(0.6.23)이라 `vendor/edgequake-llm` + `[patch.crates-io]` 로 패치. → **upstream 동기화 시 vendor/patch 재적용 필요**(핀 고정 + CI).

### 3.2 chat(추출) provider 무시
- `create_openai()` 가 `OPENAI_BASE_URL` 무시 + `gpt-5-mini` 하드코딩.
- 수정: `OpenAIProvider::compatible(key, base_url)` + env 모델 해석 → OpenRouter `qwen/qwen3.5-122b-a10b` 사용.
- **임베딩 BASE_URL 과 chat BASE_URL 분리**(임베딩=litellm, chat=OpenRouter).

### 3.3 KV JSONB 전체 GIN 인덱스 제거 (2026-06-25)
원본 `PostgresKVStorage::create_table()` 가 `eq_*_kv.value` 전체에 `USING GIN (value)` 자동 생성 → write-heavy checkpoint/lineage 저장을 심각하게 지연.

**실측 incident** (dev_wiki.md):
- 문서 `89eb9cd6-...` 의 단건 KV checkpoint upsert 가 **109.116s** 소요. 실제 chunk vector 저장은 그 직후 약 0.8s.
- `eq_eq_default_kv` = live 약 1,601행 / 약 2.4MB JSON 대비, `eq_eq_default_kv_value_gin` = **1,020MB** 비대화(런타임 읽기엔 사실상 미사용).

**결정/규칙**:
- KV primary key 인덱스 유지, `keys_with_suffix` 용 reverse-key suffix 인덱스 유지.
- `value` 전체 GIN 재도입 금지. (현행 경로는 `get_by_id`/`get_by_ids`/`keys_with_prefix`/`keys_with_suffix` key 기반 접근이 주류, community/graph 조회는 `GraphStorage`/AGE 전용 인덱스 사용.)
- JSONB 검색 필요 시 특정 key family/JSON path 의 **partial/expression index 만** 허용.
- fork 에서 신규 자동 생성 제거. 기존 dev DB 는 drop:
  ```sql
  DROP INDEX IF EXISTS public.eq_eq_default_kv_value_gin;
  ```
- 검증:
  ```sql
  SELECT indexrelname, pg_size_pretty(pg_relation_size(indexrelid)) AS size
  FROM pg_stat_user_indexes
  WHERE relname = 'eq_eq_default_kv'
  ORDER BY pg_relation_size(indexrelid) DESC;
  ```

---

## 4. 파서 라우팅 변경 (markitdown → 구조파서 분기)

- v1: PDF/PPTX/DOCX 전부 markitdown. XLS/HWP 계열은 kordoc.
- v2 실측 재확정(W6):
  - **markitdown 은 pptx/DOCX 의 병합(colspan/rowspan)을 파싱 시점에 소실**(blockify 복구 불가) → 병합 중요 pptx/DOCX 는 **OCR·VLM(:18050) structural 라우팅**(과거 kordoc/MinerU 권고와 동일 취지).
  - PDF → OpenDataLoader(`markdown_with_html=True`, `<table>` 70개 실측).
  - HWP/HWPX/HWPML → kordoc 신뢰(실측 생략, 수용된 리스크).
  - 텍스트형은 markitdown 유지.

---

## 5. 정정 사실 (drift 정리)

- **실 서버 바이너리** = 루트 패키지 `edgequake`(`cargo build --bin edgequake`), `edgequake-api`(lib) 아님.
- **§11 E2E 기록은 v1(edgequake=adaptive)** 시점 — 현행 facade 경로는 passthrough(03-dev-progress §2 참조).
- **임베딩 `:7997` 표기 드리프트**: SoT.md §3.5/§5.4 의 로컬 `:7997` 은 현행 런처보다 오래된 표기. 현행 운영 배선은 원격 litellm. (process-definition v1.0 에서 정정 반영.)
- **DSN 포트 정합**: 코드 기본 DSN `port=5432`(community.py `DEFAULT_DSN`) vs 운영 전제 `:5433` → 환경별 명시 필요.
- **edgequake base URL 정합**: search.py 기본 `:8080` vs 전용 edgequake 기동 `:8081` → 배선 일원화 필요.
- **프로세스정의서 개정 이력**(`docs/kb-pipeline-process-definition.md`):
  - v0.1 (2026-06-29): 초안 — 원본 프로세스정의서 구조(1.개요~6.협의사항) 모방, 코드 사실 반영.
  - v1.0 (2026-06-29): 검증 반영 — 임베딩 배선(원격 litellm) 정정, 4장 업무어조 복원·코드 레퍼런스 5장 강등, Search 실노출경로(`/search`) vs 라이브러리 라우터(`unified_search`) 분리, `/communities/build` 추가.
  - 이 정의서가 코드 레퍼런스(함수명/파일경로/env/유니코드)의 권위 출처이며, `_workspace/01-architecture.md 부록 A` 에 색인으로 요약됨.

---

## 6. 엑셀 게이트웨이 검증 재설계 (2026-06-29~30)

> 설계 `docs/superpowers/specs/2026-06-29-excel-gate-postparse-design.md` · 계획 `docs/superpowers/plans/2026-06-29-excel-gate-postparse.md`(v2 READY, ultracode 대립검증으로 codex 대체).

- **게이트 이동**: doc_guard 를 **파서 전(원시바이트 13규칙)** → **파서 후단(파싱 결과 기반)** 으로 이동. 기존 13규칙 전면 비활성·제거(`docguard.check` 호출 0건).
- **목적 전환**: "모든 형식 엑셀을 잘 파싱"이 최우선. 게이트는 추출이 실제로 깨지는 경우만 차단. 핵심 가정 — 파서(기본 kordoc)가 헤더 후보를 의외로 잘 찾으므로 **값이 잘못 뽑히면 헤더 오추출로 간주**.
- **차단 규칙(4 code)**: `ref_error`(#REF!/#VALUE! 등, 값+수식 양쪽 스캔) · `header_leak`(헤더가 값으로 추출) · `empty_header`(보수적·거의 비활성) · `side_by_side`(나란히 놓인 무관한 두 표). side_by_side 는 **인덱스열 중복 OR ≥2 distinct 라벨블록 비겹침 반복**일 때만(매트릭스·동명컬럼 거짓양성 제외).
- **게이트 단위 = 파일 단위**: 한 시트라도 finding 이면 파일 차단. (예: 자산목록은 NAC연계 시트의 진짜 side_by_side 로 파일 차단.)
- **provider 범위 = kb_pipeline 전용**(사용자 결정). dify/edgequake/raganything/ragflow 미적용.
- **2단계 흐름 정합(중요)**: kb_pipeline UI 는 Phase1 `parse_preview_task`(미리보기) → Phase2 `ingest_document`(pre_parsed). 게이트는 **Phase1 에 위치**(실사용 경로), Phase2 는 `pre_parsed is None` 일 때만(직접경로) 재게이트.
- **컴포넌트**: excel-parser `/parse` `stats.gate_summary` 산출(신규 `excel_parser_rag/gate/excel_gate.py`) → doc_guard `POST /v1/check-excel`(CheckReport 재사용, gate_error 합성) → knowledge_base `docguard.check_excel` + `ExcelParseResult.gate_summary`. 프론트 JobList 단계 `파싱→게이트검증→청킹→적재`, UploadPanel 문서가드규칙 패널 제거.
- **구현 브랜치(미머지)**: 7.excel-parser `feat/excel-gate`, doc_guard `feat/excel-gate`(이 세션에 git init), knowledge_base `feat/kb-pipeline-provider`.
- **비범위/후속**: 위임전결 ○매트릭스 고도화, compute_gate_summary canvas 재사용(perf), 라이브 스모크.

---

## 7. 파서 일원화 Phase 2 — 파싱 fleet in-process 통합 (2026-07-02~03)

> plan `docs/superpowers/plans/2026-07-02-parser-consolidation-phase2.md`(v3 READY) · spec `...-design.md`. 상세 진행표는 03-dev-progress §Phase 2.

**결정**: 외부 파서 서비스(excel-parser :18055, document-parser :18050, redis)를 전부 제거하고 parse-svc 단일 이미지 in-process 로 흡수. markitdown 완전 제거. 라우팅 소유를 blockify → `parse_service/router.py` 로 이전.

- **2a**: parse-svc 재구조화(`parsers/{pdf,ocr,excel,docx}` + `tools/` + `router.py`) + `chunk_needed` flag. facade `/ingest` 가 flag 로 분기(excel 자체청킹 false → 청크 그대로 insert).
- **2b**: `excel_parser_rag` 패키지 vendoring(상대임포트만, 자기참조 0) → `_fetch_rag_chunks` in-process(`get_backend(cfg.backend).parse`). 외부 excel-parser HTTP 제거.
- **2c**: document-parser VL OCR(pptx/이미지/스캔) in-process 이식(vl_api/elements_parser/image_utils/pdf_converter/prompts, config→env, gotenberg=httpx 직접, PDF 렌더=PyMuPDF). 발견·수정: ① AsyncClient 이벤트루프 재바인드(asyncio.run per-call → "Event loop is closed") ② 순수텍스트 figure→text 재분류(blockify figure→image 매핑이 markdown 유실 — 구 HTTP 경로에도 있던 잠재 결함).
- **2d**: markitdown 완전 제거(코드+requirements, 가드 `test_no_markitdown`). docx=kordoc(`tools/kordoc.py`, 병합표 `<table>` 보존), 폴백=kordoc. facade `service/parsing.py`·`excel_parser_client.py`·`ingest.py` 삭제, `/ingest/submit`·`/ingest/status` 제거(kb-backend 참조 0 확인), Dockerfile.facade JRE 제거. blockify `PARSER_ROUTING`/`recommended_parser` 삭제(W6 측정은 역사 기록 유지).
- **2e**: Dockerfile.parse-svc 에 node/kordoc 런타임(`npm install -g kordoc`, 이미지 검증 kordoc 3.8.3/java21/fitz). compose 에서 excel-parser·document-parser·redis 서비스 + redis_data 볼륨 + adaptive_chunk OCR base_url 제거, parse-svc depends_on=gotenberg+minio. 스택 down/build/up 전 서비스 healthy, xlsx(excel_rag_parser)·png(VL OCR)·docx(kordoc `<table>`) /parse·/ingest 정상 확인.

**불변식 유지**: 표 `<table>` HTML 보존 / 모달 U+3008·U+3009 byte-identical / BGE-M3 1024d / 청크 KB당 단일우주 / 단일 Postgres+RLS.

**커밋**: 2a `51692e9`..`2144f00` · 2b `f59a40b`+`8cfeb05` · 2c `7a0f980`+`ee39a66` · 2d `13c8dc0`+`a8f9818`+`f3e73f3` · 2e `fddbd2a`+`ee840dd`.

**미결(범위 밖)**: E2E 다형식 동시적재 완주는 OpenRouter LLM 처리량 지연으로 미완(확장자별 단독 indexed 는 검증됨). LLM 크레딧 여유 시 재확인.

## 8. PDF MinerU 레인 — 문서수준 게이트 (2026-07-13)

> spec `docs/superpowers/specs/2026-07-13-mineru-pdf-integration-design.md` · plan `...-plans/2026-07-13-mineru-pdf-integration.md`(v3 READY, ultracode 4렌즈 blocking 0). 배포 노트 `docs/mineru-deploy-notes.md`.

**결정**: PDF 파서에 **문서수준 게이트**(PyMuPDF triage 신호)를 붙여, 순수 텍스트 PDF 는 기존 ODL 레인, 스캔·혼합 PDF 는 **MinerU**(hybrid-http-client: PaddleOCR 로컬 layout + VLM 원격) 레인으로 분기. 기존 triage(`feat/pdf-triage`)를 게이트 신호원으로 이식.

- **게이트**(`parsers/pdf/gate.py`): triage 버킷 집계. 비어있지 않은 페이지가 전부 `TEXT_ONLY`(또는 빈/열기실패)→ODL. `OCR_NEEDED` 하나라도 있으면 MinerU `parse_method='ocr'` **강제**. `OCR_NEEDED` 없이 `LLM_NEEDED` 만(스캔 없는 텍스트+이미지)→MinerU `'auto'`.
- **MinerU 스파이크 검증(소스 직독)**: `_ocr_enable` 은 **문서당 단일 bool**(`ocr_classify`→`pdf_classify.classify`). VLM 이 항상 주 추출자, PaddleOCR 은 layout(bbox+type)+스캔시 OCR-det 보충. → 혼합 문서에 `'auto'` 두면 문서수준 classify='txt' 판정 시 스캔 페이지 텍스트 유실(2026-07-07 빈표 버그 재발) → **스캔 있으면 'ocr' 강제**로 방지.
- **레인**(`parsers/pdf/mineru_lane.py`): `_invoke_mineru`(do_parse 디스크 출력 read, mineru 지연 import)→content_list→`_content_list_to_elements`→기존 `elements_to_blocks`(표 `<table>` HTML 보존 재사용)→1-based pages.
- **폴백**(`parsers/pdf/__init__.py`): `_safe_decide_route`(gate 지연 import+try/except → pymupdf 부재/triage 예외 시 ODL, 새 500 없음). MinerU import/호출 실패 **또는 빈 결과** → ODL/in-process VL 폴백(가용성 회귀 없음). 기존 parse 본문은 `_odl_lane` 로 추출.
- **env**: `MINERU_VLM_SERVER_URL`(별도 GPU) — `scripts/parse-svc.env`(gitignored, 런처 로드). 미설정 시 폴백.

**불변식 유지**: 표 `<table>` HTML 보존 / 모달 U+3008·U+3009(blockify 경유) / page_idx 1-based / 청크 KB당 단일우주(blocks 만, 청킹=facade `/chunk`) / in-process(mineru 라이브러리 import, VLM 만 원격).

**검증**: 로컬 단위검증 35 passed(triage 10·gate 11·mineru_lane 4·routing 5·기존 pdf 5). 회귀 0(기존 red 5건=minio auto-create + 모달 `KBP_MODAL_ENRICH` 는 baseline 동일, MinerU 무관). 실 MinerU end-to-end 는 배포서버 스택검증 잔여(로컬 Intel Mac 미설치).

## 9. 동의어/구어 검색 실패 근본원인 수정 — 리랭커 (2026-07-21)

> 증상: edgequake 가 구어/동의어 쿼리("이사하는데 휴가 나오나?")에서 문서의 "거주지 이전시 : 1일"을 못 찾는데 raganything(rerank 없는 순수 벡터)은 성공. 격리실험(동일 9청크·bge-m3·122b LLM 을 raganything 워크스페이스에 적재)으로 임베딩/청킹/LLM 차이 아님을 배제.

**근본원인(edgequake 디버그 로그로 확정)**: naive 벡터 회수는 정상 — 정답 청크 포함 **11개 후보 회수(recall 문제 아님)**. 진짜 관문은 `sota_engine/mod.rs`의 하드코딩 `min_rerank_score=0.1`. 리랭커가 **긴 다주제 청크**(제4조 청원휴가에 배우자출산·정기검진·거주지이전 혼재)의 "이사" 관련성을 희석 평가 → 정답 0.05, "휴가" 키워드만 든 노이즈(빈 표헤더·지름신 flowchart) 0.28~0.35. 정답이 0.1 컷에 탈락, naive 가 노이즈 3개만 반환. 후보 총 **579토큰** vs 예산 **10000토큰** → 필터링 자체가 불필요(다운스트림 truncation 이 토큰예산으로 이미 캡). 리랭커는 **재정렬**만 하면 됨.

**수정(2개, 둘 다 필요)**:
- **9a 리랭커 교체**(`crates/edgequake-api/src/state/mod.rs` `create_bm25_reranker()`): env(`EDGEQUAKE_RERANK_BASE_URL/MODEL/API_KEY`) 있으면 neural `HttpReranker`(litellm `/v1/rerank`, Qwen3-Reranker-0.6B, Standard 포맷), 없으면 BM25 폴백. BM25 는 어휘 겹침 없는 동의어 청크에 0.0 부여 → 컷. 재정렬 품질용.
- **9b 임계값 env화**(`crates/edgequake-query/src/sota_engine/mod.rs`): `min_rerank_score` 를 `EDGEQUAKE_MIN_RERANK_SCORE` env 로(기본 0.1 유지, `min_score`의 `EDGEQUAKE_MIN_ENTITY_SCORE` 패턴 그대로). 배포는 **0.0** → 리랭커가 **필터가 아닌 재정렬 전용**. 실제 관문 해제.

**배포 env**: `docker-compose.yml`(edgequake env: RERANK_* + `EDGEQUAKE_MIN_RERANK_SCORE: "0.0"` + `RUST_LOG: ${EQ_RUST_LOG:-info}`), `service/scripts/start_dedicated_edgequake.sh`(호스트 바이너리 레인 동일 env). **API키는 `.env`(gitignore)만, 하드코딩 금지**(사용자 제약).

**검증**: 컨테이너 `kbp-edgequake-1`(:8081, facade→`edgequake:8081`) 재빌드·교체 후 — 리랭커 로그 `Using HTTP neural reranker … Qwen3-Reranker-0.6B` 확인, `EDGEQUAKE_MIN_RERANK_SCORE=0.0` 반영. "이사하는데 휴가 나오나?" → **naive 정답**(3→11 소스, "청원휴가·거주지 이전·1일"), **hybrid 정답**("거주지 이전시 : 1일" 원문+제4조 인용, 무회귀). 리랭커 배포 정상성도 별도 확인(깨끗한 문장엔 정답 0.744 vs 노이즈 0.14).

**참고(빌드)**: edgequake `docker/Dockerfile` 에 cargo 캐시 마운트(registry+target) 추가했으나, 이번 빌드는 `FROM rust:bookworm` 이 툴체인을 1.95.0 으로 플로팅해 target 캐시 무효화(전량 재컴파일 91분). 캐시 이득 보려면 rust 베이스 태그 핀(`rust:1.xx-bookworm`) 필요 — 미결.

**불변식 유지**: BGE-M3 1024d / 청크 KB당 단일우주 / 단일 Postgres+RLS. 리랭커는 검색 정렬 단계만 변경(적재·청킹 불변).

## 10. facade 가 doc_guard·MinIO 를 은닉 — `/gate/*`, `/objects/*` (2026-08-04)

> 계기: kb 가 없어지고 kbp(facade)가 유일한 API 서버로 남는다. 그런데 kb 가 doc_guard 와
> MinIO 를 **직접** 찌르고 있었다. 실측 당시 kb 는 `docguard_base_url=:8000` 을 보는데
> doc_guard 는 `:8001` 에 있어 **xlsx 적재가 게이트에서 통째로 실패**했고, 소비자 쪽에서만
> 터져 원인이 "엑셀 적재가 안 된다"로 보였다. 주소를 아는 곳이 둘이면 이런 어긋남이 늦게
> 드러난다.

**게이트** — facade `POST /gate/check-excel`, `GET /gate/rules`. doc_guard 원형 응답을
**변형 없이** 통과시킨다(소비자 `_build_gate_popup` 과 프론트가 원형 필드를 직접 읽는다).
`/parse` 에 합치지 않은 이유: 판정 룰이 바뀔 때 재파싱 없이 게이트만 다시 돌려야 한다.
순서는 **parse(gate_summary 산출) → 게이트 판정 → 청크**다.

**오브젝트** — facade `PUT /objects/{scope}/{...}`, `GET|DELETE /objects`. 키 규칙을
`service/objects.py build_key` 하나가 소유한다. 기존 객체는 마이그레이션하지 않는다:
실 버킷 1137개(page 802·original 17·staging 318)를 재현해 **1137/1137 일치, 불일치 0**.

### 제어평면만 은닉한다 (기각한 대안 포함)

| | 무엇 | 결정 |
|---|---|---|
| 제어평면 | staging put/get, 원본 승격, 페이지 이미지 쓰기, 삭제 | **facade 뒤로** |
| 데이터평면 | 브라우저의 썸네일·인용 이미지 읽기(`/obj/*`) | **현행 유지** |

데이터평면까지 facade 로 돌리는 안을 검토하다 기각했다. 실측 페이지 이미지 802개
중앙값 292KB·최대 3,987KB, 검색 1회 인용 top_k=10 이면 최대 ~4MB 가 facade 를 통과한다.
facade 는 `gunicorn -w 4` + 동기 핸들러(AnyIO 스레드풀 공유)이고 잡 대기가
`KBP_JOB_MAX_WAITERS` 만큼 스레드를 이미 점유한다 — 이미지 스트리밍이 얹히면 **썸네일을
뿌리는 동안 잡 접수·`/healthz` 가 스레드를 못 얻는다**. facade 를 정적 파일 서버로 만드는
셈이라 설계 전제와 충돌한다.

**presign 안도 기각**: 한때 presigned URL 로 가기로 했다가 사실확인에서 뒤집었다 —
`presign` 은 kb 에서 **호출부 0건 데드코드**고, 실제 읽기 경로는 프론트 Next.js 의
`/obj/* → minio/document-parser/*` same-origin rewrite 다. presign 은 `localhost:9000`
절대 URL 이라 외부/https 에서 혼합콘텐츠로 깨진다.

### 소비자(kb) 변경

- `DocGuardClient` → facade `/gate/*`. multipart `check()`(pdf·docx 전 포맷)는 **삭제** —
  호출부 0건 데드코드인데 남기면 doc_guard 직결 경로가 그대로 남는다.
- `MinioStore` → facade `/objects/*` 클라이언트로 재작성. 메서드 시그니처를 유지해 호출부
  9곳은 생성자만 바뀐다. **minio 패키지 의존성과 자격증명 4키를 제거**했다.
- kb 에 남는 것: `public_url`·`rewrite_minio_urls`·키 헬퍼(MinIO 미호출 순수 문자열) +
  `minio_bucket`(챗 답변의 옛 절대 URL 을 `/obj/` 로 치환할 때 버킷 식별용).
- staging 프리픽스 소유권이 facade 로 갔다. `MinioBlobStore` 가 또 붙이면
  `parse-staging/parse-staging/...` 이 되어 워커가 staging 을 못 찾으므로 인자로 받으면 거부.

### 폐쇄망 노출 정책 변경

doc_guard 의 `3004:8000` 을 **제거**했다(인증 없는 게이트가 밖에 열려 있었다). facade·
facade-worker 에 `KBP_DOC_GUARD_URL: http://doc_guard:8000` 을 넣어 인트라스택 DNS 로만
닿는다. 외부 노출은 facade(3000)·edgequake(3001)·webui(3002)·minio 콘솔(3003)·
parse-svc(18081)·postgres(5433) 로 줄었다.

### 검증

- 실기동 왕복: 3개 scope PUT→GET→DELETE prefix, 한글·공백·괄호 파일명 보존, 버킷 원상복귀
- kb `/docguard/rules` → facade `/gate/rules` → doc_guard: 룰 14개
- xlsx 업로드 → `status=gate_failed` + `gate_popup` 원형 보존(error 1건)
- kb 클라이언트로 3 scope 왕복 후 `delete_prefix` 2건 → 잔여 0
- kbp `service/tests` 219 passed / kb 회귀 0건(기존 실패 19건 그대로)

**전 구간 라이브(2026-08-04, 임베딩 백엔드 복구 후)**:

| 레인 | 결과 | 경로 |
|---|---|---|
| 단건 PDF | `ready` 556s (gate 0→parse 4→chunk 12→insert 379→persist_meta 556) | 원본 승격 없음(이 레인의 기존 동작) |
| 배치 PDF | `completed` 508s, `succeeded` 1건 | staging PUT/GET → **원본 승격 PUT** → staging DELETE, 전부 facade 경유 |

- 원본 객체 `c762afe0…/original/3-8. 직장 내 괴롭힘·성희롱 예방 규정(2023.10.23.개정).pdf`
  117,862B = 원본 크기 일치. `documents.minio_object` 도 같은 키. 한글·`·`·괄호 보존.
- 이번 배치 staging 은 정리됨(남은 3건은 이 변경 이전 배치의 기존 잔여물).
- 인용 이미지는 데이터평면 그대로 200 — `chunks_meta.minio_image_object` 에 기록된 키
  (`{parse-svc docs_id}/{docs_id}_{p}.jpeg`)가 MinIO 에서 읽힌다. **주의**: 이 키의
  docs_id 는 kb 의 `documents.docs_id` 가 아니라 parse-svc 의 content-hash 다.
- 인코딩: PUT 은 경로 `%20`, GET 은 쿼리 `+` 로 나가는데 facade 가 같은 키로 해석한다(확인함).

**4건 혼합 배치(2026-08-05)** — 전용 KB 에 텍스트층 PDF 3 + xlsx 1:

```
[   5s] 워커 capacity=2 → 2건 processing / 2건 queued   (수용 제어 동작)
[ 544s] 3-8 succeeded
[ 731s] 3-3 succeeded
[ 756s] 2-1.xlsx gate_failed          ← 배치 안에서 게이트 차단
[1308s] 소유권이전 succeeded → completed_with_errors (3 성공 / 1 차단)
```

facade 경유 집계(내 배치분): staging PUT 4 → GET 4 → **원본 승격 PUT 3** → staging DELETE 3.
게이트 차단된 xlsx 는 원본 승격 대상이 아니므로 PUT 3 이 맞다. 원본 3건 모두 소스와
**바이트 일치**(74,239 / 117,862 / 1,423,727B)하고 `documents.minio_object` 키가 facade 가
돌려준 키와 같다. 청크 7·18·17, 검색 응답도 정답(연차휴가 15일·1개월 개근 1일·2년마다 1일 가산).

동시에 사용자 UI 배치 2건(`3d8da3c5…`, `cb59813f…`)이 돌았는데 그쪽도 전부 facade 경유였다.

**발견(범위 밖, 기존 결함)**: `batch_worker.py:217` 이 `status == "succeeded" and canonical_path`
일 때만 staging 을 지운다. 게이트 차단·실패 항목의 staging 객체는 **영구히 남는다**. 이번
xlsx 도 남았고, 이전 배치 잔여 3건 중 2건이 같은 원인(위임전결기준표.xlsx)이다. 커밋
c785964(배치 워커 도입) 시점부터의 동작이며 이번 변경과 무관하다. facade GC 의 고아 스윕은
`kbp-jobs/` 프리픽스만 보므로 `parse-staging/` 은 수거하지 않는다.

**보류(D19)**: doc_guard 룰 14개 중 docx 13·pdf 11 로 **엑셀 전용이 아닌데**, kb 는
`xlsx/xlsm` 에서만 `check_excel` 을 부른다. 즉 pdf·docx 는 게이트를 통과하는 게 아니라
거치지 않는다. pdf·docx 게이트를 켤 때 `/gate/check` 를 함께 설계한다.

**불변식 유지**: 청크 KB당 단일우주 / 표 `<table>` 보존 / 단일 Postgres+RLS / BGE-M3 1024d.

## 11. 적재 잡 취소 + 잡 경로 기본화 (2026-08-06)

> 계기: kb 에 취소 기능이 아예 없었다. facade 는 `DELETE /jobs/{id}` 를 갖고 있었지만
> 소비자가 없었고, kb 프론트의 `✕` 는 "잡 **기록** 삭제(데이터 보존)" 라 의미가 다르다.

### 계약 — 축소범위 (a)

| 상태 | 동작 |
|---|---|
| `queued` | **즉시 `canceled`**(+ 문서·배치아이템 전이) |
| `running` | **진행 중인 그 단계는 완주**하고 다음 단계를 제출하지 않는다 |
| `stage == 'insert'` | **취소 불가**(409) — edgequake 부분 적재 방지 |
| provider ≠ `kb_pipeline` | **409** |

`running` 인 parse·chunk 의 **즉시 중단은 비범위**다. `_run_parse`·`_run_chunk` 는 진입 시
`_stage()` 를 한 번만 부르고 수백 초 다운스트림으로 들어가 취소를 다시 읽는 지점이 없다 —
다운스트림 폴링 훅이 필요하고 그건 별개 작업이다. UI 문구가 이 계약을 그대로 말한다.

### 설계 — 예외가 아니라 결과 상태

`JobCanceled` 예외로 가려던 초기안은 두 번 막혔다. pipeline 체크포인트가 예외가 아니라
**값을 반환**하고(`return _bad(...)`), `_fail` 을 건너뛰면 정리가 안 된다. 그런데 워커는
이미 `result.status` 로 분기한다(`rejected`→`gate_failed`) — 거기에 `canceled` 를 더하면
새 예외도 재-raise 도 광범위 except 우회도 필요 없다.

**취소는 `delete_doc` 을 부르지 않는다.** 취소는 insert 제출 전에만 성립해 edgequake 에
정리할 게 없는데, `_fail` 의 정리 대상은 `content_hash[:16]` 이고 중복 스킵 가드는
**파일명 기준**이라, 같은 내용을 다른 이름으로 올려 취소하면 **이미 `ready` 로 적재된 첫
문서를 지운다**(kb 행은 `ready` 로 남아 조용한 손실).

취소 API 는 **단일 원자 UPDATE** 다(facade `JobRepo.cancel` 과 같은 형태) —
`canceled`/`running`/0행 세 분기가 한 문장에서 갈린다. 읽고-판단-쓰기로 나누면 그 사이
잡이 종결돼 취소가 유실되거나 **이미 끝난 잡을 뒤집는다**(중복 스킵 경로는 외부 호출 없이
밀리초 안에 `succeeded` 를 쓴다).

### 잡 경로가 기본이 됐다

`kb_pipeline_use_jobs` 기본값 `False` → **`True`**(사용자 결정). 취소·유량제어·상태가 이
경로에 달려 있다. 롤백 레버는 `KB_PIPELINE_USE_JOBS=false` 하나.

### 검증 7라운드에서 뒤집힌 것들

plan(`docs/superpowers/specs/2026-08-06-ingest-cancel-plan.md`)이 v1→v7 로 가며 **내 설계가
여섯 번 뒤집혔다**. 기록해 둘 만한 것:

- **provider 구멍** — 취소 UI/API 가 provider 를 안 봐서 kb_pipeline 이 아닌 KB 에서
  버튼이 뜨고 플래그만 세워진 채 잡은 `succeeded` 로 끝나는 **조용한 무동작**
- **`delete_doc` 파괴적 호출**(위)
- **running 취소가 전부 409** — 조건부 UPDATE 를 `WHERE status='queued'` 로만 걸었더니
  running 은 항상 0행인데 플래그는 이미 커밋돼, "취소 불가" 를 본 잡이 몇 분 뒤 취소됨
- **배치 아이템을 무조건 `canceled`** 로 만들면 claim 이 영영 안 되어(claim 은 `queued` 만
  본다) 잡이 **영구 `running`** 으로 고착. kb 에는 잡을 회수·종결하는 코드가 없다
- **`should_cancel` 이 적재를 죽임** — 잡 기록 삭제 API 가 running 잡도 지우는데
  `_get` 이 `ValueError` 를 던진다. fail-open 이어야 한다
- **문서가 `ingesting` 고착** — 진입 가드가 잡만 종결하고 문서를 방치

### 구현 중 드러난 것 둘 (검증이 못 잡은 것)

- `_should_cancel` 을 `if _staged:` 안에 정의 → **dify 경로에서 `UnboundLocalError`**
  (호출은 분기 밖이다). 회귀 6건으로 드러났다.
- **raw SQL 의 UUID 바인딩이 sqlite 에서 0행** — `Uuid` 컬럼이 postgres 는 native,
  sqlite 는 dash 없는 32자 hex 로 저장되는데 dash 포함 문자열을 넘겼다. postgres 에서만
  우연히 맞고 **dev 에서는 취소가 통째로 무동작**이었다. Core `update()` 로 교체.
  → **테스트를 붙이자마자 첫 실행에서 드러났다.** plan 검증 7라운드가 못 잡은 종류다.

### 상태 어휘 `canceled` — 11곳

하나라도 빠지면 배지가 영문 원문으로 뜨거나 **폴링이 안 멈춘다**. 잡 8곳(모델·스키마·
types·JobList known/label/TERMINAL·JobProgressInline TERMINAL·BatchStatusPanel) +
문서 3곳(DocumentList·DetailModal·상세 page) + `.badge.canceled` CSS.
`batch_repository.TERMINAL_STATUSES` 에도 넣어야 배치가 `completed` 로 간다.
`DocumentList` 의 기본 필터 `HIDDEN` 에도 — 안 넣으면 취소 문서가 "적재된 문서" 목록에
계속 남는다.

**커밋**: kb `4bd434e`(파이프라인·워커) → `7163ed6`(API·배치·기본화) → `4a2f6b4`(프론트)
→ `1d7b909`(UUID 수정) → `c01d7ec`(테스트 23건). 634 passed, 회귀 0건.

---

## 파일 변환 API 도입 — gotenberg·kordoc(docx) 제거 + router 4분기 (2026-08-06)

**배경**: `router._domain` 의 폴백이 kordoc(docx 파서)이라 `hwp·hwpx·doc·ppt·html` 이 전부
거기로 갔고 파싱이 안 됐다. 내규 코퍼스 247건 중 **hwp 175 + doc 5 = 180건(73%)** 이 막혀 있었다.
gotenberg 는 `pptx → PDF` 한 경로 때문에 compose 서비스 1개로 남아 있었다(이미지·PDF 스캔은
각각 `image_utils`·PyMuPDF 라 gotenberg 불필요).

**결정**: 한컴 도큐먼트툴즈 기반 **원격 변환 API**(`docs/API_FILECONVERT_AGENT.md`)로 교체.
폐쇄망에도 동일 API 가 들어간다(사용자 확인) → gotenberg 완전 제거.

```
excel   xlsx xlsm xls        자체 청킹 (변환 금지 — 시트 구조 손실)
ocr     png jpg …            이미지 직행, PAGE_HYBRID(전사 + 시각 서술)
text    txt md csv json …    평문 그대로 (신설, utf-8-sig/utf-16/cp949)
pdf     그 외 전부            변환 API → ODL/GW/VL
```

**변환은 `run_parse` 에서 한다 — `route()` 안이 아니다.** `route()` 의 `filename` 은 값 복사라
`_render_and_upload`(페이지 이미지)에 전파되지 않는다. 거기 두면 파싱은 17페이지인데
`page_count=1` 이 되어 `page_spans` 와 어긋나고 인용 링크가 죽는다(검증에서 잡힌 회귀).

**kordoc 은 docx 경로에서만 뺐다.** `excel_parser_rag` 의 기본 백엔드가 kordoc 이라
(`config.py:66`) 전면 제거는 불가하다. Dockerfile 의 `npm i -g kordoc` 도 유지.

**kordoc 업그레이드는 하지 않는다** — 계층 오파싱이 4.7.1 에서도 그대로다.
실측(신한자산신탁 정관 hwp 17p):

```
                실텍스트    헤딩                 리스트
kordoc 3.8.3    10,610자   55개 전부 ###           0     章·條 같은 레벨(평평)
kordoc 4.7.1    10,610자   55개 전부 ###           0     개선 없음
변환API → ODL   10,600자   #1 / ##2 / ###1       200     계층 있음
```

4.x 의 "8단계 항목부호"는 `markdown → HWPX` **생성** 방향이고 추출 방향은 안 고쳐졌다.

**라이브 검증**(정관 hwp): 17페이지 · 262블록 · 14,282자, `page_count == page_spans == 17`,
`convert_ms=3,313` · `parse_ms=2,736`. 순서도 이미지(webp)는 PAGE_HYBRID 로
`시작 → 지름신 강림 → (YES) …` 분기까지 서술됐다.

**알려진 한계**: 변환 API 가 단일 실패점이다(폴백 없음 — 잘못된 파서로 조용히 가는 것보다
명시적 실패를 택했다). pptx 는 처리 경로가 바뀌어(gotenberg+VL 전량 → triage 라우팅) 회귀
가능성이 있고 미검증이다.

### 병합셀 docx 대조 — 콘텐츠 손실 없음, 표 경계 재구성됨 (허용) (2026-08-06)

`2-7-1-1. 고객확인의무 업무방법서`(원본 XML `<w:tbl>` 21개, gridSpan 118·vMerge 266)로
kordoc(구) vs 변환API→ODL(신)을 대조했다.

```
원본 docx XML         표 21개
kordoc(구 경로)         표 9개    — 여러 표를 하나로 병합해 과소 집계
변환API→ODL(신 경로)     표 44개   — 표 하나가 최대 4조각으로 분리(6곳, 조각 14개)
```

셀 텍스트 자체는 유실되지 않는다(표0 사례: 헤더 2행 + 데이터 1행으로 분리되지만 내용은 온전).
**표 경계가 원본과 다르게 재구성**되지만 청킹 시 헤더 컨텍스트가 인접 청크에 남아있을
가능성이 높아 콘텐츠 손실보다는 구조 재배치로 판단, **허용**하기로 확정(사용자 결정).
kordoc으로 되돌리지 않는다 — kordoc도 원본과 다른 표 개수를 내므로 우열이 명확하지 않다.

### DRM(Fasoo) 해제 지원 추가 (2026-08-06)

`docs/REFERENCE_DRM해제_API.md` 스펙으로 `parse_service/tools/drm.py` 를 신설했다.
fileconvert.py 와 같은 인프라(base host)를 쓰지만 path prefix 가 달라(`/api/drm/agent/tool`
vs `/api/fileconvert/agent/tool`) 별도 `KBP_DRM_URL` env 로 뺐다. **토큰은 값이 fileconvert 와
현재 동일하지만 별도 env(`KBP_DRM_TOKEN`)로 분리**했다 — 사용자 지시("혹시 모르니까 따로
변수 놔줘, 값은 우선 동일하게") — 서버가 나중에 분리될 가능성에 대비.

`run_parse`(app.py) 의 변환 단계 **앞**에 `drm.is_drm(file_bytes)` 매직바이트
휴리스틱(`DRMONE`, 실측: 길이-프리픽스 2바이트 뒤에 온다)으로 게이트해 DRM 파일만 원격
호출한다(비-DRM 파일 매 요청마다 왕복 추가하지 않음). 해제 후 바이트는 여전히 원래
포맷(hwp/docx/pdf 등)이므로 기존 fileconvert 변환 단계가 이어서 처리한다 — 순서:
**DRM 해제 → (필요시) 포맷 변환 → 라우팅**. `timing_metrics.drm_ms` 로 계측.

**라이브 검증**: `docs/1. 자금집행 요청서 및 동의서.pdf`(Fasoo DRM 래핑, `file` 명령이
"OpenPGP Public Key"로 오판)로 `/parse` 호출 → `drm_ms=268.6`, `convert_ms=0.0`(해제 후
이미 PDF), `page_count=1`, `n_blocks=20`, `enriched_content` 4,291자. 해제된 PDF 1페이지는
텍스트 레이어가 없는 스캔 이미지 1장(PyMuPDF `get_text()` 빈 문자열, `get_images()`==1) —
정상(스캔 서식 특성), VL/OCR 경로가 승계해 콘텐츠를 뽑아냈다.

### PDF 트리아제 임계치·레인 env화 + 페이지별 판정 로그 (2026-08-06, plan v3 READY)

이미지 파서 고도화 준비 — 사용자 지시: "트리아제 룰 임계치는 환경변수화 한다. 임계치
설정값·임계치 도달 시 갈 레인 둘 다 소스 변경 없이 조정 가능해야 한다." + "지금 판정 로그를
남기고, 나중에 env 조정만으로 라우팅을 바꿀 수 있게 준비." plan(ultracode 경쟁 검증 3라운드,
v1→v3 READY, `/Users/xxx/.claude/plans/mighty-whistling-quiche.md`)대로 구현. **동작
불변**(env 미설정 시 기존 하드코딩 값과 100% 동일) — 527 passed(506 기존 + 21 신규), 회귀 0.

- `parse_service/parsers/pdf/triage.py`: `classify()` 6개 파라미터(`mixed_image_cov`/
  `content_min`/`diagram_curve_min`/`diagram_line_min`/`diagram_img_count`/
  `diagram_combo_curve_min`) + `extract_signals()`의 `has_native_text` 임계(20)를 `None`
  sentinel + 호출시점 env 읽기로 전환(`KBP_TRIAGE_*` 7개). 호출부(`gate.py`)는 무변경 —
  `test_pdf_gate.py`가 `triage_document`를 단항 lambda 로 monkeypatch 하므로 호출부에
  kwargs 를 추가하면 안 됨.
- `parse_service/parsers/pdf/gate.py`: `_VL_RATIO` 모듈로드시점 읽기를 `decide_route()`
  호출시점으로 이동(`KBP_GATE_VL_RATIO`, float 파싱 실패 시 경고+0.5 폴백 — triage 예외
  처리와 별개 try/except). 레인 선택 3곳도 env 화(`KBP_GATE_VL_LANE`/`KBP_GATE_OCR_LANE`/
  `KBP_GATE_DEFAULT_LANE`) — 값은 `{odl,vl,paddle_gw}` 화이트리스트 검증, 밖이면 경고+그
  변수 고유 기본값 폴백. 이 보장 덕분에 `__init__.py`의 `decision.lane=="vl"` 등 기존
  리터럴 비교는 무수정으로 재배선을 그대로 인식한다(§B 핵심 — ultracode adversarial-break
  렌즈가 v2 라운드에서 잡아낸 결함, __init__.py 를 안 고쳐도 되는 이유를 코드 주석으로 명시).
  `RouteDecision.page_signals` 필드 순수 추가 — triage 성공한 모든 경로(전부 SKIP/빈
  페이지 `total==0` 분기 포함)가 채움, `triage_document()` 예외 경로만 `()`(공유 싱글턴
  `_ODL`). `total==0`은 의도적으로 `KBP_GATE_DEFAULT_LANE`을 적용하지 않고 `lane="odl"`
  리터럴 고정(분석할 신호가 없는 축퇴 케이스를 vl/paddle_gw로 보내는 건 위험).
- `parse_service/parsers/pdf/__init__.py`: `_parse_routed()`를 단일 종료점으로 재구성
  (기존 `log.exception`/`log.warning` 위치·문구·조건은 한 글자도 안 바꿈 — `pages=None`
  선-초기화로 "예외 실패" vs "빈 결과 실패"를 구분 유지). 반환 직전 `_log_triage_table()`
  호출 — `decision is None`(게이트 실패)이나 `page_signals` 없음이면 짧은 안내 로그만
  남기고 종료, 그 외엔 페이지별 마크다운 표(`| p | triage | dia | char | img | imgcov |
  curve | line | 판정근거 | 성공여부 | 실패시 재시도(fallback) 여부 | 선택 fallback |`)를
  찍는다. `KBP_TRIAGE_LOG_TABLE=0`으로 완전 스킵 가능. 이중 try/except 격리(로그 버그가
  파싱을 절대 못 깨게).
- env 12개(`KBP_TRIAGE_*` 7 + `KBP_GATE_*` 4 + `KBP_TRIAGE_LOG_TABLE`)를 `.env.example`/
  `.env.airgap.example`/`scripts/parse-svc.env.example`(bare `KEY=값`)과
  `docker-compose.yml`/`docker-compose.airgap.yml`(`${VAR:-값}`)에 각 파일 실제 컨벤션대로
  추가. `KBP_GATE_VL_RATIO`는 코드엔 이미 있었지만 이 5개 파일 어디에도 문서화가 안 돼
  있었다(ultracode completeness-and-tests 렌즈가 grep 으로 확인한 실측 — 이번에 처음 추가).

### 가로형 페이지 → LLM_NEEDED + 페이지 전사 프롬프트 PAGE_HYBRID 통일 (2026-08-06, plan v5 READY)

사용자 지시: "문서가 가로형 문서이면 묻고 따질 것도 없이 그냥 VL로 가자" + (확인 질문에 이어)
"PAGE 전사는 가로형뿐 아니라 전체적으로 통일해야 한다 — 표·순서도·차트 프롬프트 통합". plan
(ultracode 경쟁 검증 5라운드, v1→v5 READY — v1/v2 는 diagram_pages 배제 회귀를 3개 독립
렌즈가 수렴 지적해 재설계, v4 는 "DIAGRAM_USER_PROMPT가 표/본문 규칙이 없다"는 잘못된 전제로
다이어그램 보충까지 통합하려다 2개 렌즈가 additive-모드 중복 회귀를 잡아 되돌림).

- `parse_service/parsers/pdf/triage.py`: `PageSignals.is_landscape`(파생, `width>height`)를
  `extract_signals()`가 채운다. `classify()`는 **다이어그램 판정을 최우선으로 유지**하고
  landscape 는 mixed/text_only/OCR_NEEDED/SKIP 을 대체하는 차선 조건으로 끼운다 — 진짜
  다이어그램(curve/line/img 임계 충족)이면서 가로형인 페이지도 `is_diagram=True`를 그대로
  얻어 `gate.py`의 `diagram_pages` 집계에서 안 빠진다(v1 즉시-return 설계였다면 빠졌을
  결함). `KBP_TRIAGE_LANDSCAPE_TO_LLM`(기본 1)로 규칙 자체를 끌 수 있다.
- `parse_service/parsers/pdf/__init__.py`: `_ocr_elements_for_page`의 `else` 분기(비-다이어
  그램 호출)가 이제 밋밋한 기본 프롬프트 대신 `PAGE_HYBRID_SYSTEM_PROMPT`/`PAGE_HYBRID_
  USER_PROMPT`를 쓴다 — `_vl_lane`(문서수준 vl 레인)과 `_odl_lane`의 스캔페이지 일반 OCR
  둘 다 적용(페이지를 **처음부터** 전사하는 경로). `_supplement_diagram_pages`(다이어그램
  보충, `diagram=True`)는 **그대로 `DIAGRAM_*` 유지** — ODL 레인에서 이미 있는 네이티브
  블록에 additive 로 얹는 경로라 PAGE_HYBRID(표/본문/그림을 전부 재분해하는 범용 프롬프트)
  로 바꾸면 표/본문이 중복되는 회귀가 생긴다(ultracode 가 잡은 결함, 상세는
  `docs/superpowers/specs/2026-08-06-triage-landscape-deferred.md` D2). `diagram`/`else`
  분기 구조 자체는 안 바꿔서(반환값만 교체) 기존 테스트가 전부 무변경으로 통과.
- 회귀 0(535 passed, 1 skipped — 기존 527 + 신규 8건 triage/prompt 테스트).

**후속 — 순서도 라벨 보존 프롬프트 수정 + env화 (2026-08-06)**: 라이브 검증(실제 OpenRouter
VL 호출) 중 순서도 박스 라벨("접수/검토/승인")이 "첫 번째 단계/두 번째 단계/세 번째 단계"로
일반화되는 문제를 발견 — `prompts.py`의 순서도 조항을 "박스 안 라벨은 이미지에 보이는 글자
그대로 옮긴다"로 명시 강화. **원인 진단 중 최초 재현 PDF 자체의 결함도 발견**: PyMuPDF 기본
폰트가 한글 글리프를 지원하지 않아 라벨이 렌더링 단계에서부터 읽을 수 없는 점(dots)으로
나왔던 것 — 모델은 그 이미지에 대해 그럴듯한 라벨을 지어낸 것이었다(할루시네이션). 한글
폰트(AppleGothic)를 임베드해 재현 PDF를 다시 만들자 원래 프롬프트도 문제없이 라벨을
보존했고, 강화된 프롬프트도 동일하게 정확했다 — 즉 실제 결함은 "모델이 라벨을 못 지킨다"가
아니라 "테스트 이미지 자체가 읽을 수 없었다"였음을 실측으로 확인.

- `parse_service/parsers/ocr/prompts.py`: 순서도 조항을 `_DEFAULT_PAGE_HYBRID_DIAGRAM_RULE`
  상수로 분리하고 `KBP_PAGE_HYBRID_DIAGRAM_RULE` env로 전체 교체 가능(소스 수정 없이 프롬프트
  튜닝). `page_hybrid_prompts()` 함수를 신설해 **호출 시점에** env를 읽는다(이번 세션
  확립된 관례). 기존 `PAGE_HYBRID_SYSTEM_PROMPT`/`PAGE_HYBRID_USER_PROMPT` 정적 상수는
  하위호환으로 유지하되 모듈 로드 시점 값이라 env 변경을 반영하지 않는다는 점을 문서화 —
  실제 소비자(`pdf/__init__.py`의 `_ocr_elements_for_page`, `ocr/__init__.py`의 이미지
  도메인 `parse()`) 둘 다 `page_hybrid_prompts()`로 전환.
- env 1개(`KBP_PAGE_HYBRID_DIAGRAM_RULE`, 기본 빈 문자열=코드 기본값)를 5개 env 파일에
  문서화. dotenv는 여러 줄 값을 지원하지 않아 값은 줄바꿈 없이 한 줄로 적어야 함을 명시.

---

## 12. global 검색 노출 — 명시 mode 토글 (2026-08-09)

`kb_pipeline/search.py` 의 커뮤니티 map-reduce(`global_search`)는 W3 이후 라이브러리로만
존재했다(app.py 미import). 이번에 **facade `/search` → kb 챗 → 프론트 토글**까지 배선했다.

### 12.1 자동 라우터(`route()`)를 쓰지 않은 이유

`unified_search`/`route()`(GLOBAL_CUES 단서어 + tiny LLM 타이브레이크)는 **의도적으로
배선하지 않았다.** 오판 비용이 비대칭이다 — 넓은 질문을 local 로 보내면 부실한 답이지만,
좁은 질문을 global 로 보내면 **최악 6분 + LLM 비용 6회**를 태운다. 휴리스틱 한국어 단서어
목록의 정확도를 그 비용에 걸 근거가 없어, 사용자가 버튼으로 고르게 했다. `route()` 는
라이브러리에 그대로 남아 있다.

### 12.2 동시성은 `threading.Semaphore` 가 아니라 DB 카운터

global 요청 하나는 map N + reduce 1 의 **순차** LLM 이라 `(N+1)×timeout` 을 점유한다.
gunicorn `-w 4` 에서 모듈 스코프 `Semaphore` 는 **프로세스마다 따로 세어** 전역 상한이
되지 못한다(실효 상한 = 설정값 × 워커수). 그래서 `kbp.global_search_slots` 행 카운터 +
`pg_advisory_xact_lock` 으로 옮겼다.

advisory lock 이 장식이 아니라는 것은 실측으로 고정했다 — 잠금을 빼면 커밋 전 INSERT 가
다른 트랜잭션의 `count(*)` 에 안 보여 **상한 2 인데 5개가 승인**된다(TOCTOU).
`test_global_search_pg.py::test_concurrent_acquire_never_exceeds_the_limit`.

슬롯 TTL = `(GLOBAL_TOP_K_MAX+1) × timeout × 2`. **하드코딩하면 안 된다** — TTL 이 실제
소요보다 짧으면 살아있는 슬롯이 청소되어 상한 자체가 사라진다.

### 12.3 전용 LLM 타임아웃

`get_text_llm(*, timeout=None)` 으로 **키워드 전용 + 기본값** 인자를 추가했다(무인자 기존
호출자·테스트 monkeypatch 보존). global 만 `KBP_GLOBAL_LLM_TIMEOUT`(기본 60s)을 쓴다 —
적재 관례값 300s 를 그대로 쓰면 요청 하나가 최악 30분을 점유한다. 프로세스 전역 env 를
호출 시점에 바꾸는 방식은 같은 워커의 다른 경로와 경합하므로 금지.

### 12.4 오류 의미론 — 침묵하는 거짓 안내 금지

- `reports_exist`: **테이블 부재만 fail-open("없음")**, 그 외 `psycopg.Error` 는 raise.
  DB 장애를 "리포트가 아직 없다" 로 위장하면 사용자가 야간 배치를 기다리게 된다.
  app.py 가 그 예외를 **503** 으로 바꾼다.
- LLM/httpx 실패는 **422**. kb 클라 재시도 조건이 `429 or >=500` 이라 5xx 로 주면
  6분짜리 요청이 3배로 증폭된다.
- `newest_report_time` 은 `(newest, oldest, count)` 를 준다. `newest` 만 노출하면
  `community_reports` 에 DELETE 가 없어 **삭제된 문서 기반 리포트가 잔존**하는 상태
  (1건만 어제, 37건은 두 달 전)가 "어제 기준" 으로 읽혀 거짓 안심을 준다. 프론트는
  `oldest` 가 30일을 넘으면 경고를 띄운다.

### 12.5 프론트 — 토글은 kb_pipeline 에서만

백엔드는 `mode` 를 **provider 분기 안에서만** 읽으므로 dify/ragflow KB 에 버튼을 보여주면
거짓 기능이 된다. `provider` 가 미해결(로딩/오류)인 동안에도 숨겨 local(기존 동작)로
안전 폴백한다.

`frontend/lib/api.ts` 의 `chat()` 은 global 턴에만 `AbortController` 420초를 건다. ⚠️ 이
타임아웃은 **UI 보호용이지 비용 절감이 아니다** — 끊어도 facade 는 map 루프를 계속 돌린다.

응답→턴 매핑과 신선도 판정은 `frontend/lib/chatMode.ts` 로 **React 없이** 분리했다.
frontend 에 테스트 러너가 없어(`scripts` = dev/build/lint/typecheck) TSX 는 node 로 돌릴 수
없는데, 이 판정이 틀리면 **조용히 거짓 안심**(낡은 리포트를 최신처럼, 빈 답변을 정상처럼)을
주기 때문에 실행 검증이 필요했다. `npm run test:chatmode` 가 tsc 로 컴파일한 **실제 출하
코드**를 불러 14건을 검증한다(복사본 검증 함정 회피). 되돌리기 5종 전부 빨강 확인.

### 12.6 배포 전 게이트 — `scripts/check_global_rank.py`

"눈으로 확인" 을 **한 줄로 돌리는 스크립트**로 바꿨다. 선정 단계(`_rank_reports`)는
순수 파이썬이라 **LLM 이 필요 없다** — Postgres 만 있으면 되고, 게이트웨이가 죽어 있어도
검사할 수 있다.

    python scripts/check_global_rank.py --dsn "$KBP_PG_DSN" --list
    python scripts/check_global_rank.py --dsn "$KBP_PG_DSN" \
        --workspace <eq-ws-uuid> --specific 실제질문.txt

**실측으로 드러난 것(2026-08-09)** — 처음 세운 판정 기준("겹침 0 이 절반 넘으면 보류")이
틀렸다. `_rank_reports` 는 질문 토큰의 **부분문자열 겹침**으로 고르는데:

- **넓은 질문은 겹침 0 이 정상이다.** `"이 지식베이스는 전체적으로 무슨 내용인가?"` 는
  리포트 본문의 구체어를 쓰지 않으므로 0 이 되고, 그때 `rank` 순(= 큰 커뮤니티 순)으로
  고르는 것은 "전체 요약" 용도에 오히려 맞다. 이걸 결함으로 세면 정상을 잡는다.
- **좁은 질문에서는 조사·어미가 선정을 망친다.** `"이사할 때 필요한 절차가 있나?"` →
  토큰 `{이사할, 때, 필요한, 절차가}`. 리포트가 "거주지 이전 및 주소 변경 절차" 여도
  `절차가` 는 `절차.` 의 부분문자열이 아니라 **겹침 0** → 무관한 큰 커뮤니티로 답한다.
  더 얄궂게, `"주소 변경 절차"` 는 정답을 고르는데 `"주소변경 절차"`(붙여쓰기)는
  **오답을 고른다** — 띄어쓰기 하나로 뒤집힌다.
  (같은 뿌리의 실패를 전에 겪었다: "이사" 질문이 "거주지 이전시 1일" 을 못 찾은 건.)

그래서 스크립트는 질문을 **넓음/특정 두 부류로 갈라** 판정한다 — 넓은 질문의 겹침 0 은
통과, 특정 주제 질문의 겹침 0 은 보류. 이 결함은 **답변만 봐서는 절대 드러나지 않는다**
(LLM 이 주어진 리포트로 성실히 요약하므로 답이 그럴듯하다).

**남은 사람 몫**: `--specific` 에 넣을 **실제 사용자 문장**. 자동 생성 질문은 리포트
제목에서 뽑으므로 관대해서 통과한다(실측 4/4 통과) — 진짜 실패는 사용자가 쓰는 어투에서만
나온다. 그 목록은 도메인을 아는 사람만 만들 수 있다.

**완화책은 이미 배선돼 있다** — UI 토글 설명이 global 을 "넓은 질문" 용도로 안내하고,
특정 주제 질문은 local 로 유도한다. `_rank_reports` 자체를 고치는 것(형태소 분석·임베딩
기반 선정)은 이 작업 범위 밖이다 → `deferred.md`.

### 12.7 검증

- kbp `service/tests/ + tests/`: **383 passed, 1 skipped**(기준선 368 → +15, 회귀 0).
  `test_global_search_pg.py` 13건은 실 Postgres 필요(`requires_pg`).
- kb `backend/tests/`: **651 passed, 16 failed**(기준선 648/16 → +3, 회귀 0).
- frontend: typecheck·lint·build 통과 + `test:chatmode` 14건.
- **실 PG 테스트만 잡은 버그**: `_acquire_global_slot` 이 `cur.fetchone()["count"]` 로
  읽었는데 bare `psycopg.connect` 는 `row_factory=dict_row` 가 없어 `TypeError`.
  JobRepo 에는 있어서 착각한 것 — DSN 을 직접 여는 코드에는 없다.
