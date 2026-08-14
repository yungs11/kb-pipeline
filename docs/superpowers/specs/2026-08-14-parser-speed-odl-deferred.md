# 2026-08-14 — 파서 속도개선(ODL A/C/B) deferred

상태: **후행작업으로 연기**(사용자 지시 2026-08-14). 브랜치 `feat/parser-speed`(워크트리
`.claude/worktrees/parser-speed`, base `321a094`) 는 **코드 변경 0** 상태로 남겨둔다.
plan 원본: `~/.claude/plans/parser-speed-abc.md` (v1, ultracode 검증 NEEDS_REVISION).

## 왜 미뤘나

ultracode 4렌즈 경쟁 검증(2026-08-14, 440k 토큰)이 **제시 근거 자체를 뒤집었다** —

| | 착수 전 판단 | 검증 결과 |
|---|---|---|
| A | "조건 한 줄, 위험 거의 없음" | **output-neutral 아님.** 일부 문서가 오히려 느려진다 |
| B | "pages 인자 + 인덱스 매핑" | **견적 3~4배.** 성립 여부 자체가 미측정 |
| 합격기준 | `enriched_content` byte-identical | **VL 비결정성 때문에 성립 불가** |

재개할 때 아래 must-fix 를 plan v2 에 반영한 뒤 다시 검증한다.

## must-fix (검증 종합, 19건)

1. §1-7·§5: 함수명·라인 인용 정정. 실제는 parse_service/app.py:148 `def _render_and_upload(` (`_pages` 없음, 호출부 :343, docstring 참조 :259/:421). 응답 키는 :367 page_count / :368 pages / :369 page_spans. plan 전역에서 `_render_and_upload_pages`·`:153-201` 표기를 모두 교체한다.

2. §5 처방 코드 위치 수정 — 게이트를 함수 진입부가 아니라 `if ext == "pdf":`(app.py:172) 분기 **안쪽**으로 내린다. 진입부 early-return 은 렌더 비용이 0 인 비-PDF 경로(:192-202, page_count=1 고정)까지 죽여 test_parse.py:555-566(.txt page_count==1)·:458(업로드 무관 페이지 메타 유지) 계약을 이득 없이 깬다. §5 표의 '렌더를 끄면 page_count=0' 서술도 'PDF 한정'으로 한정한다.

3. §2 에 'A 는 output-neutral 이 아니다' 를 명시한다. 오늘 odl 레인 0 인 문서도 ODL 이 돌기 때문에 pdf/__init__.py:584 `len(odl_md) != total_pages` 가드가 발동해 문서 전체가 `_odl_lane`(:591) 으로 위임될 수 있고(test_parser_pdf_routing.py:140 이 이 경로를 고정), A 이후 odl_md=[] 이라 이 분기가 **구조적으로 도달 불가**가 된다 → 같은 문서가 전 페이지 VL 전사로 바뀐다. 동시에 :568 except 의 odl_error 트레이스와 :959 `_odl_lane` 의 ToolError→ParserError 경로도 소멸해 실패율·관측 필드가 바뀐다. §2 에 '페이지수 불일치 문서에서 산출 경로가 교체된다(의도된 변화)' + 'odl_error/parse_failed 경로 소멸' 을 적고, A-P2 를 '위임 로그 0건인 문서에 한해' 로 재정의하며, ODL 페이지수 강제 불일치 픽스처로 이 분기를 명시 검증하는 항목을 A 합격 기준에 추가한다.

4. §2 '영향 범위 — 전량 VL 문서에 한정' 문장을 정정한다. 새 조건은 `odl_pnos or skip_pnos or total_pages==0` 이므로 바뀌는 문서군은 '전량 VL' 이 아니라 **odl 레인 0 ∧ skip 레인 0 인 모든 문서** — `paddle_gw + vl` 혼합 문서(스캔+가로형)가 포함된다. §7 측정 코퍼스 3건(3레인합성/온톨로지/정의서)에 paddle_gw+vl 조합이 없으므로 해당 문서 1건을 코퍼스에 추가하고 A-P3(레인 분포·transcribe 집합 불변)을 그 문서로도 돌린다.

5. A-P2 / B-P2 / V2·V4·V7 의 합격 기준 'enriched_content byte-identical' 을 구조 동등성으로 교체한다. 측정 대상(온톨로지18p·정의서15p)은 전량 VL 이고 VL 은 원격 LLM 이라 동일 입력에도 산출이 흔들려(2b-2 `_fail_if_vl_empty` 재시도 정책이 그 전제) sha256 비교는 항상 불합격이 되거나 임의 면제된다. 대체 지표: page_traces 의 페이지별 src 시퀀스(:792 라벨 포함), 페이지 수, 페이지당 블록 타입 구성, table_blocks/n_blocks, `_page_markdowns` 호출 유무. byte-identical 은 결정적 대상인 C-P1(도구 반환값)과 VL 을 안 타는 디지털 텍스트 문서에만 적용한다고 명시한다. (V7 도 '측정 문서 = VL 미경유 디지털 텍스트 1건' 으로 못박는다.)

6. §3 C·§4 B 의 `threads=`/`pages=` kwarg 를 무조건 전달하지 않도록 처방을 바꾼다. 미지원 버전의 ODL 이면 opendataloader.py:46 `except Exception` → ToolError → pdf/__init__.py:568 `except Exception` 흡수 → odl_md=[] → 전 문서 VL 전사(비용 폭증)로 조용히 열화하고, `_odl_lane`(:959) 경로 문서는 ParserError 로 전건 실패한다. 폐쇄망에서만 발생한다. 처방: `inspect.signature(opendataloader_pdf.convert)` capability probe(또는 TypeError 시 kwarg 없이 1회 재시도) + threads 는 기본값일 때 kwarg 자체를 넘기지 않음. 아울러 §8 V6 의 '폐쇄망 ODL 이 pages/threads 를 지원하는지 V6 에서 확인' 은 사실과 다르다 — scripts/airgap/verify-bundle.sh:202-278 check_imports 는 kordoc 바이너리(:220-226)와 xlsx 왕복(:238-274)만 보고 opendataloader_pdf 를 import 조차 하지 않는다. verify-bundle 에 이미지 안에서 `opendataloader_pdf.convert` 시그니처를 확인하는 블록을 추가하는 작업을 plan 항목으로 넣는다(메모리 '가드는 있었는데 안 돌렸다').

7. §3 `_ODL_THREADS = os.environ.get(...)` 를 모듈 로드 시점 상수에서 **함수 내부 매 호출 조회**로 바꾸고 정수 파싱 실패 시 1 로 클램프(+경고 로그) 한다. 모듈 상수는 monkeypatch.setenv 단위 테스트를 불가능하게 만들어(importlib.reload 필요) C 의 유일한 자동 검증 수단을 없애고, `auto`/`4 ` 같은 값이 그대로 라이브러리로 넘어가 위 항목과 같은 경로로 전 문서 VL 전사가 된다. 또한 '기본값 "1" = 현행 동작 불변' 서술은 convert_generated.py:159-160 `if threads:` 때문에 성립하지 않는다(CLI 에 `--threads 1` 이 실제로 붙는다) — '기본값이면 인자 미전달' 로 고쳐 적는다.

8. C-P1 을 '문서당 threads=N 을 **≥3회 반복** 실행해 회차 간 산출이 서로 byte-identical 이고 threads=1 결과와도 동일' 로 강화한다(opendataloader.py:42 라이브러리 자체가 비결정성을 경고 — 1회 비교는 flaky 성질을 검출할 수 없다). 스캔 페이지 혼합 문서를 필수 포함한다.

9. §3 C 게이트에 '산출물 변동 시 첫 파괴지점' 관측을 합격 항목으로 추가한다: (1) :584 위임 로그 0건(페이지 분할 개수가 흔들리면 문서 전체가 `_odl_lane` 으로 위임돼 ODL 을 두 번 돌리고 산출물이 통째로 바뀐다), (2) thin_pnos 집합 불변(공백 한 칸 차이로 :643 `_digital_text_len(_md(n)) < _DIGITAL_MIN_CHARS` 판정이 뒤집혀 페이지가 odl_md 분기에서 VL 전사(:777)로 이동하고, VL 빈 응답이면 :406 `_fail_if_vl_empty` 로 문서 전체 ParserError). 또한 C-P2(≥20% 단축)를 개발 호스트가 아니라 **동일 CPU/메모리 제한 컨테이너(또는 폐쇄망)** 에서 재측정하는 항목을 넣는다(JVM `availableProcessors` 오버서브스크립션/OOM 위험).

10. §4 B-0 을 부분 추출 케이스로 재정의한다. 현행 B-0('3페이지 PDF 전체 변환에서 sentinel 치환 여부')은 전체 변환에서 절대==상대라 B-R1 이 의존하는 성질을 구분하지 못한다. 반드시 `pages="2,3"` 로 실행해 sentinel 이 **절대 페이지번호(2,3)** 인지 **추출분 상대번호(1,2)** 인지 확정한다 — 상대번호면 `set(odl_md) != odl_requested` 가 정상 문서에서 항상 오발(B-P3 위반)하거나 md 가 엉뚱한 페이지에 붙는다. 함께 '같은 페이지의 full-run md vs subset-run md 동일성'(ODL 은 문서 단위 파이프라인이라 표 연속·헤딩 번호·레이아웃 통계가 페이지 간 컨텍스트를 탄다)을 코퍼스 다건으로 비교하고, 불일치면 **B 를 조기 제외**한다고 적는다. 부수: §1-5 의 `cli_options_generated.py:118 %%page-number%%` 는 argparse help description 의 `%` 이스케이프일 개연성이 높으니 `convert_generated.py:62 %page-number%` 를 우선 후보로 두고, 현행 PAGE_SEP(opendataloader.py:15 `<<<ODL_PAGE_BREAK>>>`)에는 플레이스홀더가 전혀 없다는 사실을 표에 병기한다.

11. §4 B-1 의 변경 범위를 전수로 다시 쓴다. (a) opendataloader.py:44 `markdown_page_separator=PAGE_SEP` 를 sentinel 포함 형태로 바꾸면 같은 상수를 쓰는 :61 join 구분자 / :64 literal split / :65-66 선두 빈 조각 휴리스틱이 모두 깨진다(동적 구분자면 :64 가 안 쪼개져 문서 전체가 1페이지로 뭉치고, :61 join 은 페이지번호 없는 경계를 만들어 md 파일이 2개 이상일 때 키를 잃는다) → :58-67 재작성(regex split + 다중 md 파일 처리)을 명시. (b) 이 분리기는 `pages=None` 전체 경로(:959 `_odl_lane`)에도 적용되며 :963-964 `for i, md in enumerate(md_texts): page_number = i + 1` 은 위치 기반이라 dict 계약과 충돌한다 → `_odl_lane` 페이지번호 산출 변경도 항목에 넣는다. (c) odl_md 관련 라인 전수는 :546(`odl_md: list[str] = []` seeding — 타입/초기값), :568(대입), :578(예외 리셋), :584(가드), :586(로그 인자 — 의미 변화 주석), :598(`total_pages = len(odl_md)` → `max(odl_md, default=0)`), :636(`odl_md[pno-1]` 인덱싱 → 키 조회). plan 이 열거한 ':584, :598' 은 불완전하다.

12. §4 B-1 에 테스트 갱신 목록을 명시한다(현재 한 줄도 없고 V5 는 '실패 0' 만 요구). 최소: parse_service/tests/test_tools_opendataloader.py:12 픽스처(literal PAGE_SEP 사용)·:16 `assert pages == ["page-1",...]`·:70 `test_parse_promotes_to_parser_error`; 리스트 반환 스텁 test_parser_pdf.py:8/:19/:50/:85/:106, test_parser_pdf_routing.py:47(`wire` set_md 본체)/:108/:196/:245/:419/:588/:594/:601/:609/:614/:618; 인덱스 정렬 고정 test_parser_pdf_routing.py:129; :584 가드 동작 고정 test_parser_pdf_routing.py:140(B-R1 이 판정식을 바꾸므로 재작성 대상). 또한 `_page_markdowns` 에 `pages=` 키워드를 추가하면 기존 스텁 시그니처 `lambda fb, fn:` / `def boom(fb, fn)` 가 TypeError 를 던지고 이 TypeError 가 :568 `except Exception` 에 흡수돼 **테스트가 엉뚱한 이유로 통과**할 수 있다는 점을 함께 적는다.

13. §4 B-2 에 요청 페이지 집합의 상한·실패 처리를 적는다. `odl_requested` 는 triage(PyMuPDF) 기준 total_pages 에서 나오는데 ODL 이 보는 페이지 수가 그보다 적을 수 있다(:584 가드의 존재 이유가 바로 그 불일치). 범위 밖 번호를 `pages=` 로 넘겨 ODL 이 에러를 내면 :46 → :568 흡수 → odl_md=[] → 전 페이지 VL 전사로 조용히 전환된다. 상한 클램프와 '부분 요청 실패 시 전체 재시도할지/실패로 둘지' 를 명시한다.

14. §5 P-R1 / §8 V7 의 '하류 계약을 새로 만들지 않는다' 를 정정한다. 렌더를 끄면 `pages=[]` 인데 service/jobs/runner.py:184 → service/adaptive_chunk.py:106-108 에서 빈 리스트는 `is not None` 이라 **body 에 명시 전송**된다(service/tests/test_chunk_endpoint.py:339 이 'None 이면 미포함, 빈 값은 포함' 을 계약으로 고정). 지금은 예외 경로에서만 드물던 상태가 상시화된다. 둘 중 하나를 plan 에 못박는다: (a) 렌더를 끌 때 pages 를 `None` 으로 보내 미전송을 택하거나, (b) V7 에 `KBP_RENDER_PAGE_IMAGES=0` 으로 parse→chunk 까지 돌려 청크 수·페이지 매핑이 렌더 켠 경우와 동일한지 비교하는 항목 추가.

15. §5 폐쇄망 절의 verify-bundle 경고 조건을 구현 가능한 형태로 바꾼다. 'KBP_RENDER_PAGE_IMAGES=0 인데 KB 인용을 쓰는 구성을 경고' 는 .env 에 '인용 사용 여부' 키가 없어 판정 불가 — 조용히 거짓인 가드가 된다(CLAUDE.md 경계 사항). `KBP_FAIL_ON_EMPTY_PAGE=0` 블록(verify-bundle.sh:116-119)과 같이 **값이 0이면 무조건 경고(페이지 이미지·page_count 소멸 명시)** 로 단순화한다. 또한 verify-bundle.sh:76-83 이 docker-compose.airgap.yml 의 `${VAR:?}` 패턴을 필수 키로 자동 파생하므로, compose 에는 반드시 `${KBP_ODL_THREADS:-1}` / `${KBP_RENDER_PAGE_IMAGES:-1}` 형태로 쓰고 `:?` 를 쓰지 않는다는 제약을 §5 에 적는다(기존 현장 .env 파손 방지).

16. §7 측정 방법에 노이즈·캐시 통제를 넣는다. (a) ODL 은 JVM 서브프로세스라 단계마다 parse-svc 를 재기동하는 현 절차에서 '변경 후' 첫 측정이 항상 콜드다 → **1회 폐기 후 3회 중앙값**. (b) 시간순 1회 전후 비교 대신 **인터리브(before,after,before,after)** 로 VL 게이트웨이 지연 드리프트를 개선으로 오독하지 않게 한다. (c) A-P1·B-P1 의 '감소' 를 수치화한다 — 1차 지표를 parse_ms 가 아니라 **ODL 구간 벽시계(및 호출 유무)** 로 승격하고 parse_ms 는 '중앙값 ±1σ 내에서 악화 없음' 보조 지표로 내린다(VL 이 parse_ms 의 85% 인데 A 의 기대효과는 2.3~5.2s 라 잡음에 묻힌다). §1-6 표에는 '직전 세션 값 — 이번 라운드 재측정 대상' 을 병기해 합격 기준이 옛 수치에 고정되지 않게 한다.

17. §8 에 신규 자동 테스트 항목을 추가한다(현재 V1 은 '기존 테스트 실패 0' 뿐이고 A·C·§5 의 핵심 주장은 전부 수동 실측이라 커밋 후 회귀를 못 막는다). 최소: (1) A — test_parser_pdf_routing.py:104 `test_pure_scan_document_skips_odl` 를 본뜬 '전량 vl 레인 문서 → `_page_markdowns` 미호출' + 대칭 'odl/skip 이 1페이지라도 있으면 여전히 호출' + 'gate 실패(total_pages==0)면 여전히 호출'(:598 폴백 보호), (2) C — 기본값에서 kwarg 미전달 / env 오버라이드 전달 / 비정수 클램프 / 미지원 버전 폴백, (3) §5 — `KBP_RENDER_PAGE_IMAGES=0` 에서 PDF 는 (0, []) 이고 렌더 함수가 호출되지 않음, 비-PDF 는 기존대로 page_count==1, 기본값 1 에서 기존 계약 유지(test_parse.py:438·:458 픽스처 재사용).

18. §1-2 표 제목 '`odl_md` 소비처 — 전수 3곳' 을 정정한다. `:790` 은 odl_md 를 참조하지 않고 지역변수 `md` 를 읽는 분기이며(`elif _digital_text_len(md) >= _DIGITAL_MIN_CHARS:`), 문자열 "odl_md" 가 나오는 곳은 :792 의 트레이스 라벨이다. 실제 참조 전수는 :546 :568 :578 :584 :586 :598 :636. 라벨과 변수를 섞은 서술이 B 의 영향 범위 판단 근거를 흔든다. 함께 §2 A-R1 / §9 의 블로킹 항목 'gate.py page_lanes 전수 할당 여부' 는 **확인 완료(전수)** 로 닫는다 — gate.py:105-106 `total_pages=len(sigs)` + 조건 없는 comprehension, triage.py:207-222 결번 없는 append, 전-SKIP 조기 return(gate.py:109-116, 회귀 앵커 :111-114, test_pdf_gate.py:230)까지 page_lanes/total_pages 를 채우고, 유일한 미커버 상태(열기 실패의 page_lanes=(), total_pages=0)는 :566 의 `total_pages==0` 항이 이미 흡수한다. 도달 불가 조건항 `(set(range(1,total_pages+1)) - set(lanes))` 추가 계획은 삭제한다.

19. §6/§8 에 마무리 절차를 검증 항목으로 올린다(현재 본문 언급만 있고 V 에 없어 '코드만 바꾸고 문서 방치' 로 끝난다): 완료 후 `_workspace/02-changes.md`(결정·계약 변화)·`_workspace/03-dev-progress.md`(진행/리스크) 갱신, 제외된 단계(A/B/C 중 게이트 미달분)의 `deferred.md` 기록. 아울러 V1 테스트 경로가 `parse_service/tests kb_pipeline/tests service/tests` 뿐이므로 루트 `tests/`(test_modal*.py, test_blockify.py 등) 를 포함하거나 '제외 — 이유' 를 명시한다.

## 범위 밖으로 분류(deferred of deferred)

D1. kb-backend 의 `page_count`/`pages[].minio_object` 실제 소비 지점 확인 — 이 워크트리에 kb-backend 코드가 없어(edgequake 서브모듈 grep 0건) 정적으로도 검증 불가. P-R1 을 제품 결정으로 사용자에게 넘긴 처리는 타당하고 기본값 1 유지로 현행 배포는 무영향. `KBP_RENDER_PAGE_IMAGES=0` 을 실제로 켜기로 결정하는 시점에 필요.

D2. service/adaptive_chunk.py:67-72 docstring 계약 불일치 — docstring 은 `pages=[{page_number, markdown}]` 을 기대한다고 적지만 parse-svc(app.py:186-190)가 실제로 보내는 것은 `{page_number, page_uuid, minio_object}` 다. 렌더 on/off 와 무관한 기존 불일치이며 외부 청킹 서비스(:18060) 소관이라 파서 속도 범위 밖. 청크→페이지 매핑을 손대는 작업에서 정리한다.

D3. 동일 PDF 의 이중 래스터화 제거 — parse_service/app.py:174 의 전 페이지 300dpi 렌더와 parsers/pdf/__init__.py:646·:972 의 파서 내부 선택 렌더가 같은 PDF 를 최소 2회 렌더한다. 렌더 결과를 공유·재사용하면 §5 의 on/off 스위치보다 이득이 크지만 이번 목표(ODL 낭비 제거) 밖. 렌더 비용이 실측에서 지배항으로 올라오면 착수.

D4. VL 동시성 개선 — parse_ms 의 85% 를 차지하는 지배항이지만 plan 이 스스로 비범위로 선언했다. A/B/C 로 걷어내는 ODL 은 2.3~32s 수준이라 전체 개선폭의 상한이 낮다. 단, A-P1 합격선을 측정 노이즈보다 크게 수치화하는 부분은 범위 내로 판단해 must_fix(측정 방법론)에 흡수했다.

D5. A 이후 `odl_error` 관측 소멸에 따른 폐쇄망 ODL 가용성 조기 탐지 — java/opendataloader 누락 이미지를 배포했을 때 전량 VL 문서만 처리하는 동안 경고가 없다가 혼합 문서에서 뒤늦게 터진다. 다만 대체 수단(verify-bundle 의 opendataloader 시그니처 확인 블록)을 must_fix 에 넣었으므로, '런타임 상시 헬스 신호' 수준의 추가 장치는 범위 밖으로 미룬다.

---

## ⚠️ 재개 전 필독 — VL 폴백 체인과 연동된다 (2026-08-14 추가)

속도개선 **A**(`:566` 의 `vl_pnos` 항 제거)는 이제 **혼자 판단하면 안 된다.**

같은 날 도입한 VL 폴백 체인(`KBP_VL_FALLBACK_CHAIN`, 기본 ON)의 **odl 단계가 `odl_md` 를
쓴다**. `:566` 에서 `vl_pnos` 를 지우면 vl-only 문서에서 `odl_md=[]` 가 되어 그 단계가
**구조적으로 도달 불가**가 된다 — 체인이 조용히 반쪽이 된다.

`v1` 은 이 항을 스위치에 묶으려 했으나(`vl_pnos and _fallback_on()`) 검증에서 **철회**했다:
`:566` 은 원래 `vl_pnos` 를 무조건 포함하므로 그 변경은 "연동 신설"이 아니라 **OFF 경로의
현행 동작 축소**이고(정합가드 무력화·thin 판정 변화·VL 호출량 변동), 배포 게이트
"스위치 OFF = 현행 동작"을 스스로 깬다.

**A 를 재개할 때는 체인의 스위치 상태와 함께 설계할 것.** 짝 문서:
`docs/superpowers/specs/2026-08-14-vl-fallback-chain-deferred.md`
