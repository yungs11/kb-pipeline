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


## 10. 스캔 레인 layout 기반 그림·차트 처리 — Plan A (2026-08-02~03)

### 문제
스캔 레인(`paddle_gw`)이 게이트웨이 markdown 만 소비해 **순서도·차트가 낱말 조각으로 흩어졌다**
(LICO p10: 22블록/평균 23.5자). 총 글자수·평균 블록길이·bbox 분포 세 신호로 "부서진 순서도"와
"짧은 불릿 본문"을 가르려 했으나 **셋 다 실패**(ABL p40 불릿 평균 25.8자로 오탐).

### 전환점 — 게이트웨이 layout
게이트웨이가 `layout[].blocks[]`(`block_label`/`block_bbox`/`block_content`)를 노출하면서
**영역 라벨로 판별이 가능**해졌다. 26페이지 실측에서 다이어그램 검출 5/6, 대조군 오탐 0.

### 결정
```
스캔 페이지에 image/figure/chart 가 (면적 5% 이상) 있으면
  → 그 페이지를 통째로 VL 1회 (PAGE_HYBRID = 기존 전사 프롬프트 + 그림·차트 조항)
  → paddle 의 type=="table" 블록은 원래 순서대로 승계
  → 나머지를 VL 출력으로 교체
없으면 → 현행 그대로
```
- **영역별 crop 이 아니라 페이지 통째**: ABL p17 은 다이어그램 2개 중 왼쪽만 검출되고 오른쪽은
  text 10개로 분해된다. crop 은 왼쪽만 서술하지만 전면 VL 은 양쪽 다 복원했다.
- **표 정본은 paddle**: 전면 VL 이 웹 스크린샷형 표(ABL p33 Re-TACRED 5행/42셀)를 세 번 다 놓쳤다.
- **프롬프트는 신규 작성 금지**: 새로 쓰면 전 페이지 `<table>` 0개 + pipe 평탄화(불변식 위반).
  기존 프롬프트에 조항만 append 하면 51셀/rowspan·colspan 보존.
- **`use_chart_recognition` 은 켜지 않는다**: 게이트웨이 기본값이 off 이고, 영역 검출과 데이터
  인식은 독립이다(라벨 개수 동일). 켜면 간트가 4,981자 거짓 행렬을 만든다(페이지 46,610자).
- **면적 하한 0.05**: 실측 참양성 최소 5.06% / 차단 대상 0.54%(법원통지서 QR). 여유가 1.01배라
  좁다 — 표본이 쌓이면 0.02~0.03(공백 한가운데)으로 하향 검토. 0.01 이하는 QR 이 통과한다.
- **면적 미상은 fail-closed**: 발동은 회귀 위험, 미발동은 현행 유지 — 불확실하면 현행을 택한다.
- **모델 `qwen/qwen3.5-122b-a10b`**: 표·스캔전사·다이어그램에서 235b 와 동등하거나 우위.
  9b 는 간트를 215셀 표로 전사해 탈락.

### 코드 변경
`gate.py`(`ocr_pages` 순수 추가) / `pdf_pages.py`(`page_numbers`) / `paddle_gw.py`(layout 노출) /
`prompts.py`(`PAGE_HYBRID_*`) / `pdf/__init__.py`(`_hybrid_scan_pages`) /
`vl_api.py`·`ocr/__init__.py`(모델·`max_tokens` 배선·프로바이더 차단).
**문서수준 라우팅 구조는 건드리지 않았다** — 페이지수준 혼합 라우팅은 Plan B 로 분리.

### 개발환경 함정 (폐쇄망에선 무관)
OpenRouter 가 프로바이더를 비전 지원 여부와 무관하게 로드밸런싱한다. 122b 5개 중 **DeepInfra 만
`prompt_tokens=42` 로 이미지를 폐기**하고 텍스트만 추론해 환각을 낸다. 거부가 아니라 조용한 환각이라
상류 검출이 불가능하다 → `KBP_VL_BLOCK_PROVIDERS`(기본 DeepInfra)로 차단.
