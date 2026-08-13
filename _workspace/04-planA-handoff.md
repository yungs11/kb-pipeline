# Plan A 인수인계 — 스캔 레인(paddle_gw) layout 기반 그림·차트 처리

> **작성 2026-08-02.** 설계는 끝났고 **구현은 아직 한 줄도 안 됐다.** 이 문서만 읽고 이어받을 수 있게
> 썼다. 활성 plan 은 `~/.claude/plans/mighty-whistling-quiche.md`(Plan A), 보류 plan 은
> 같은 폴더 `planB-pdf-page-level-routing.md`.
>
> **먼저 §2(반복된 실패 유형)를 읽어라.** 설계 검증에만 12라운드가 걸린 이유가 거기 있다.

---

## 1. 무엇을 왜 만드는가

### 문제
스캔 PDF 레인(`paddle_gw`)은 게이트웨이 markdown 만 소비한다. 그래서 스캔 페이지의 **순서도·차트가
낱말 조각으로 흩어져 의미가 사라진다**(LICO p10 실측: 22블록/평균 23.5자, "10월"이 고아 토큰).

총 글자수·평균 블록길이·bbox 분포 세 신호로 "부서진 순서도"와 "짧은 불릿 본문"을 가르려 했으나
**셋 다 실패**했다(ABL p40 불릿 평균 25.8자로 오탐).

### 전환점
게이트웨이가 `layout[].blocks[]`(`block_label` / `block_bbox` / `block_content`)를 노출하면서
**영역 라벨로 판별이 가능**해졌다.

### 해법 (한 줄 요약)
```
스캔 페이지에 image/figure/chart 영역이 (면적 5% 이상) 있으면
  → 그 페이지를 통째로 VL 에 1회 보내고(PAGE_HYBRID 프롬프트)
  → paddle 이 만든 <table> 블록은 그대로 살린 뒤
  → 나머지 블록을 VL 출력으로 교체한다.
없으면 → 현행 그대로(paddle).
```

### 왜 영역별 crop 이 아니라 페이지 통째인가
ABL p17 은 다이어그램 2개 중 **왼쪽만** `image` 로 잡히고 오른쪽은 text 10개로 분해된다.
영역별 crop 은 왼쪽만 서술하고 오른쪽을 놓친다. 전면 VL 은 양쪽 다 복원했다(R3).

---

## 2. ⚠️ 반드시 먼저 읽을 것 — 이 작업에서 반복된 실패

### 유형 A — "적어놨지만 그 계층에서는 절대 발동하지 않는 코드"
설계 문서에 규칙을 쓰고 검증까지 통과했는데, **실제 호출 사슬에서는 조건이 영원히 거짓**인 경우가
반복됐다. 실제 사례:

| 사례 | 왜 발동 안 했나 |
|---|---|
| 절단 감지를 `category=="figure"` 로 판정 | `ocr/__init__.py:77-84` 가 반환 **전에** `"text"` 로 재라벨 → 항상 False |
| `max_tokens` 를 `_build_payload` 만 고쳐 전달 | 호출 사슬 5단계 어디에도 인자가 없어 값이 도달 못 함 |
| `figure`+html 가드를 `elements_to_blocks` **뒤**에 배치 | 변환 후 블록엔 `category`/`content` 키가 없음 |
| 표 백필을 문자열만 뽑고 `block_bbox` 로 정렬 | 그 dict 에 bbox 키가 없어 정렬 키가 항상 `inf` |
| `layout_pages`/`visual_pages` 를 seeding + 로그만 | `+= 1` 하는 곳이 없어 항상 0 |

**대응**: 새 규칙을 쓸 때마다 **호출 사슬을 끝까지 따라가서** 그 계층에 그 필드가 실제로 존재하는지
확인하라. 특히 `element`(category/content 보유)와 `block`(type/text/table_body 보유)은 **다른 계층**이다.

### 유형 B — 방어 장치가 자기 근거를 무효화
지적을 막으려 가드를 넣었다가 설계의 핵심 근거를 파괴한 사례:
- **분량 급감 가드**(VL 출력이 paddle 의 절반 미만이면 실패로 간주) → LICO p3 은 paddle 46,610자 /
  VL 156자라 **항상 발동** → 없애려던 거짓 행렬이 영구 보존. 검증 합격 기준도 통과 불가가 됐다.
- **면적 임계를 0.05 → 0.20 상향** → R10(스캔)만 보고 "1~34% 표본 0건"이라 판단했는데, **R1 의
  정의서 p5(15.2%, 참양성)를 빠뜨린 오류**였다. 0.20 이면 그 페이지가 탈락한다.

**대응**: 가드를 추가하기 전에 **R1~R11 실측치를 대입해 계산**하라. 특히 임계는 R1(네이티브)과
R10(스캔)을 **합쳐서** 봐야 한다.

### 유형 C — 범위 증식
A+B 를 합친 plan 이 v19~v22 네 라운드 연속 blocking 8건을 받았고, 그 결함이 **전부 B 쪽 배관**
(렌더 캐시·레인 강등·지연 ODL·동시성·실패 계약)에서 나왔다. A 만 떼어내자 지적이 급감했다.

**대응**: **§6 범위 대장 밖은 손대지 마라.** 사용자 명시 지시다("설계 범위가 목표보다 늘어나면 멈춰").
필요해 보이면 멈추고 물어라.

---

## 3. 코드 함정 (전부 실제 확인함)

| 위치 | 함정 |
|---|---|
| `ocr/__init__.py:77-84` | `figure` + markdown만 있는 element 를 **반환 전에 `text` 로 재라벨**. 소비 지점에서 `figure` 로 판정하면 항상 실패 |
| `elements_parser.py:88-102` | VL JSON 파싱 실패를 **빈 결과가 아니라** `figure` element 1개(markdown=원문)로 만든다 |
| `vl_api.py:355-361` | JSON 파싱 실패 시 예외가 아니라 **가짜 성공** 합성: `{"content": "[Error: Failed to parse API response - …]"}` |
| `vl_api.py:167` | `VL_MAX_TOKENS` 기본 **2000**. 8000 은 `scripts/parse-svc.env:19`(호스트 dev)에만 있고 compose 엔 없다 |
| `blockify.py:376-377` | table 이 아닌 element 의 markdown 을 **통짜 text 블록 1개**로 만든다. "표 포함 시 drop" 하면 본문 산문이 함께 사라진다 |
| `blockify.py:348-360` | `figure`+html 을 `{type:"image", img_path:""}` 로 매핑하며 html/markdown 을 **버린다** |
| `pdf_pages.py:52-54` | **한 페이지 예외로도 문서 전체 `[]`** 반환 |
| `pdf/__init__.py:190` | `_supplement_diagram_pages` 의 `rendered` 는 **시퀀스**이고 그 줄은 `try`(:194) **밖**. dict 를 넘기면 문서 전체 500 |
| `pdf/__init__.py:188-189` | `rendered is None` 일 때만 지연 렌더. 캐시를 항상 넘기면 렌더가 봉인된다 |
| `pdf/__init__.py:102-106` | paddle 성공 경로는 `try`(:96) **밖**의 `else:` 블록. 여기 넣는 코드는 자체 try 필수 |
| `paddle_gw.py:149-153` | 게이트웨이 **개별 페이지 실패는 키를 남긴다**(`return pno, []`). "키 없음"만 보면 놓친다 |
| `ocr/__init__.py:93-101` + `vl_api.py:88-111` | VL 은 **스레드 안전하지 않다**. `asyncio.run` 으로 매번 새 루프 + 락 없는 `_http_client` 싱글톤 + loop-bound `_VL_SEM`. ThreadPoolExecutor 로 감싸면 예외가 per-page try 에 삼켜져 **기능이 조용히 무효화**된다 |
| `gate.py:64` | 스캔 페이지 **1장만 있어도** 문서 전체가 `paddle_gw` 레인. `pages` 에 네이티브 페이지가 섞인다 |
| `triage.py:81-94` / `:104-108` | `is_diagram`/`LLM_NEEDED` 는 `has_native_text` **안**에서만, `OCR_NEEDED` 는 **밖**에서만 → `diagram_pages ∩ ocr_pages = ∅` |

---

## 4. 실측 근거 R1~R11 (재현 가능)

측정물은 **`_workspace/planA-measurements/`** 에 있다.
- `layout/L_*.json` — 네이티브 26페이지 게이트웨이 응답(§A6 테스트 픽스처로 바로 쓸 수 있다)
- `layout/S_*.json` — 실 스캔 10페이지
- `layout/C_off_*.json` — `use_chart_recognition=false` 비교군
- `vl-out/` — VL 출력 원문(G2_=전면 hybrid, Q9_=9b, R_=모델 회귀)
- `scripts/` — 측정 스크립트 전부. `MODEL_API_KEY` 는 `scripts/parse-svc.env` 에서 읽는다

| # | 내용 | 핵심 수치 |
|---|---|---|
| **R1** | layout 라벨 판별력(네이티브 26p) | 다이어그램 6장 중 5장 검출, 대조군 **오탐 0**. `image` 의 `block_content` 는 전부 빈 문자열 |
| **R2** | 전면 VL 이 부서진 다이어그램 복원 | ABL p39/p22/p4 — 3단계 분기, C1~C9 계층, 방사형 사례 전부 복원 |
| **R3** | 혼합형(p17) — 페이지 단위를 택한 이유 | 영역 crop 은 왼쪽만, 전면 VL 은 양쪽 다 + 7행 표 유지 |
| **R4** | 차트는 전사 금지, 3줄 요약 | chart recognition ON 시 간트가 **4,981자 거짓 행렬**(페이지 46,610자) → 요약 시 156자. **`len(VL) ≪ len(paddle)` 은 정상이다 — 분량 가드 금지** |
| **R5** | 표 정본은 paddle | ABL p33 Re-TACRED 5행/42셀을 전면 VL 이 **3번 다 놓침**. paddle 은 `table` 라벨로 온전히 냄 |
| **R6** | 프롬프트를 새로 쓰면 표 계약이 깨짐 | 신규 프롬프트 → 전 페이지 `<table>` 0개, rowspan/colspan 소실. **기존 프롬프트 + 조항 append** 하면 51셀/병합 보존 |
| **R7** | 모델 `qwen/qwen3.5-122b-a10b` | 스캔전사·표·다이어그램 전부 235b 와 동등하거나 우위(제목·헤딩 보존). **미검증**: 폐쇄망에서 reasoning 을 못 꺼 max_tokens 잠식 위험 |
| **R8** | (개발환경) OpenRouter 가 이미지를 버림 | 122b 5개 프로바이더 중 **DeepInfra 만** `prompt_tokens=42` 로 이미지 폐기 후 환각. 폐쇄망엔 없는 문제 |
| **R9** | 표 백필 출처 | recog **off** 면 게이트웨이 markdown 의 표 = layout.table (1=1, 1=1, 0=0). **paddle 블록이 곧 정본** |
| **R10** | 실 스캔 10p 에서 판정 안전성 | 법원통지서 p1(글자 1,075자 + QR **0.54%**) → paddle 유지 ✅. 그림 지배 3장(34.6~43%) → 전면 VL ✅. **오탐 0**. 직인은 `seal` 라벨이라 면적이 아니라 **화이트리스트가** 걸렀다 |
| **R11** | `qwen3.5-9b` 탈락 | 표·스캔전사는 동등, ABL p33 표는 오히려 잡음. 그러나 **간트를 215셀 표로 전사**(R4 위반) |

### 면적 임계 근거 (0.05) — **여유가 좁다. 반드시 읽어라**
`planA-measurements/layout/` 덤프에서 페이지별 **최대 visual 블록 면적**을 전수 계산한 결과:
```
참양성 15건   75.1 59.5 42.6 41.4 39.4 38.3 34.6 34.6 29.9 25.3 15.7 15.2 13.6 7.4 **5.06** %
0.55 %  ABL p21 CQ흐름   ← 참양성인데 임계 아래(미포착)
0.54 %  법원통지서 p1 QR  ← 차단 대상
```
**실제 공백은 0.55%~5.06%** 이고 0.05 는 상단에 붙어 **참양성 최소값 대비 여유가 1.01배뿐**이다.

값은 0.05 로 둔다 — 위험이 비대칭이라 **미발동 = 현행 유지 = 회귀 아님**이기 때문이다.
**§V5 에서 5% 부근 참양성이 관측되면 0.02~0.03(공백 한가운데)으로 하향을 검토하라.
0.01 이하는 금지** — 0.54% QR 이 통과한다.

**임계를 건드릴 때 주의**: R10(스캔)만 보고 정하지 마라 — v4 에서 그렇게 했다가 R1 의 정의서 p5
(15.2%)를 탈락시킬 뻔했다. 항상 위 전수 목록으로 계산하라
(`planA-measurements/layout/` 에 스크립트 없이도 재계산 가능한 원본 JSON 이 있다).

### chart recognition 은 켜지 마라
게이트웨이 **기본값이 off** 이고(`opts` 미전송 = 현행), 영역 **검출**과 데이터 **인식**은 독립이다:
```
              recog ON                    recog OFF (= 기본 = 현행)
LICO p3   46,610자 · chart라벨 3      1,093자 · chart라벨 3
ABL p33    5,657자 · chart라벨 3      3,968자 · chart라벨 3
```
라벨 개수가 같으니 판정은 그대로 작동하고 거짓 행렬만 사라진다. **`opts` 를 보내지 않는다.**

---

## 5. 구현해야 하는 것

전체 명세는 `~/.claude/plans/mighty-whistling-quiche.md` 의 §A0~§A7 에 있다. 아래는 요약이다.
**반드시 plan 원문을 읽고 구현하라** — 아래는 지도이지 명세가 아니다.

| 절 | 파일 | 요지 |
|---|---|---|
| §A0 | `parsers/pdf/gate.py` | `RouteDecision` 에 `ocr_pages: tuple = ()` **추가만**. lane 판정 로직 무변경 |
| §A1 | `pdf_pages.py` | `render_pdf_pages` 에 `page_numbers`·`dpi`. **`page_number` 는 문서 절대 1-based 유지**(재열거 금지) |
| §A2 | `parsers/pdf/paddle_gw.py` | `_post_page_once` **와 `_post_page` 둘 다** tuple 반환(md, layout, page_size). `run_paddle_gateway` 반환에 `layout`·`page_size` 키 **추가**(기존 키 유지) |
| §A3 | `parsers/ocr/prompts.py` | `PAGE_HYBRID_*` = **`build_system_prompt()`/`build_user_prompt()` + 조항 append**(신규 작성 금지 — R6) |
| §A4 | `parsers/pdf/__init__.py` | paddle 분기 `else:` 안에 hybrid 단계. **자체 try 필수** |
| §A5 | `parsers/ocr/vl_api.py`, `parsers/ocr/__init__.py` | 모델 122b · `max_tokens` **5단계 전 구간 배선** · `KBP_VL_BLOCK_PROVIDERS` |
| §A6 | 테스트 | `test_parser_pdf_hybrid.py` 신설 + 5개 파일 갱신 |
| §A7 | 문서·compose·airgap | env 는 **기본값 포함 형태**(빈 문자열이 "설정됨"이 되면 `model=""` 로 전량 실패). 단 `KBP_VL_BLOCK_PROVIDERS` 만 **콜론 없는** 형태 — 폐쇄망에서 빈 문자열 opt-out 이 가능해야 한다 |

### §A4 처리 순서 (핵심)
```
1. VL 1회 호출 (PAGE_HYBRID, max_tokens=KBP_VL_PAGE_MAX_TOKENS 8000)
2. 실패 판정 → paddle 원본 유지 + warn
   a. 예외/렌더 부재   b. elements 빈 리스트
   c. 가짜 성공 형태: elements 1개 & category in {figure,text} &
      · "[Error:" 로 시작            → error_placeholder
      · **```json 펜스를 벗긴 뒤** "{" 시작 + json.loads 실패 → truncated
        (elements_parser 는 파싱 전 펜스를 벗기지만 실패 시 fallback 엔 원문을 넣는다)
   ※ 분량 비교 가드는 두지 마라 (R4)
3. keep = paddle 의 type=="table" 블록 (원래 순서). adopt_vl_table = (len(keep)==0)
4. element 수준: table → adopt면 유지/아니면 제거 / figure+html → adopt면 table로/아니면 제거 /
   그 외 → hybrid_to_blocks(markdown or text)   ← elements_to_blocks 통짜 사용 금지
5. block 수준: table 블록 adopt 여부로 유지·drop. **텍스트 블록은 절대 drop 금지**
6. vl_blocks 비면 paddle 원본 유지. 아니면 entry["blocks"] = keep + vl_blocks
   ※ heading 승계하지 않는다 — PAGE_HYBRID 가 제목을 전사하므로 중복된다
```

### 판정식
```python
VISUAL = {"image", "figure", "chart"}
_label(b) = (b.get("block_label") or "").strip().lower()     # 소문자 정규화 필수
# image/figure/chart 전부 면적 하한 KBP_VL_VISUAL_MIN_AREA(0.05) 적용
# 면적 미상(page_size 부재/bbox 이상/0값) → fail-CLOSED(False) + area_guard_skipped 증가
# 대상은 ocr_pages 에 든 페이지만 (§A0)
```

---

## 6. 범위 대장 — 이 밖은 손대지 마라

**사용자 지시**: "설계 범위가 목표보다 늘어나면 멈춰. A 의 기존 목표범위를 벗어나면."

A 가 건드리는 것은 **§A0~§A7 여덟 개가 전부**다. 새 항목이 필요하면 **멈추고 사용자에게 물어라.**

**명시적으로 A 밖 (= Plan B)**:
`decide_route` 의 lane 판정 · `_VL_RATIO` · `vl` 레인 삭제 · 페이지수준 레인 혼합 · 페이지별 병합 ·
정합 가드 · `_parse_routed` 흐름 재작성 · 렌더 캐시 클래스 · 레인 강등 · 지연 ODL ·
`_odl_lane`/`_supplement_diagram_pages` **동작** 변경 · VL 동시성 · 문서당 VL 상한 · `extract_page_texts`

**사용자가 승인한 범위 밖 항목 2개** (근거를 plan 범위 대장에 기록):
- §A0 `ocr_pages` — "혹시 모르니까 냅둬"
- §A5 `KBP_VL_BLOCK_PROVIDERS` — "이번 범위에 넣고"

---

## 7. 검증 절차 (plan §V1~§V7)

**§V3-pre 를 반드시 먼저 하라.** layout 스키마는 **측정으로만 알고 레포에 근거가 없다**(게이트웨이는
외부 서버, 현행 코드는 `body.get("text")` 만 읽는다). 실 응답 1건을 덤프해 확인할 것:
(a) `block_bbox` 단위가 픽셀인지 0~1 정규화인지 — 정규화면 면적이 `≪0.05` 라 전 페이지가 걸러진다
(b) `layout[0].width/height` 존재 여부 (c) `detection[]` 원소의 키 이름(`label`/`coordinate` 가정 중)

| | 내용 |
|---|---|
| V1 | 단위 테스트 전량 + `parse_service/tests` 전체 |
| V2 | 하위호환 — layout 없는 응답에서 §A4 가 no-op, 기존 테스트 통과 |
| V3 | 실 스캔 라이브(R10 표본 그대로 `/parse`) — 법원통지서 `hybrid=0`, 부동산교재 p7/p49 `hybrid=2` |
| V4 | 시각 페이지 회귀 — 네이티브 PDF 를 **이미지-only 로 재래스터화**해야 `OCR_NEEDED` 가 된다(스크립트는 plan 에) |
| V5 | 계측 판독 — `truncated=0`, `area_guard_skipped=0`, **`layout_pages>0` 인데 `visual_pages==0` 이면 실패**(조용히 꺼진 상태), `vl_extra_tables=0` |
| V6 | 대형 스캔 1건 — 소요시간 대 1800s 여유 |
| V7 | 폴백 — 게이트웨이 미설정/빈 결과/§A4 전 페이지 실패에서 문서 성공 |

---

## 8. 알려진 트레이드오프 (수용하기로 한 것)

- **`has_visual == False` 인 다이어그램은 놓친다** (ABL p39/p22/p4 형 — 박스마다 글자가 든 그림).
  기하 지표 2차 판정안(좌정렬 컬럼 수 ≥ 6, 양성 3/3·오탐 0/10)이 있으나 **양성 표본 3장**이라 보류.
- **읽기 순서**: paddle 표가 페이지 앞쪽으로 당겨진다(`keep + vl_blocks`).
- **표 내용이 산문으로 중복될 수 있다** — VL 이 표를 pipe 없는 산문으로 다시 낼 때. 소실이 아니라 중복.
- **VL 표가 paddle 보다 많으면 초과분 소실 가능** — 미관측(4/4 페이지에서 없었음)이라 규칙을 만들지
  않고 `vl_extra_tables` 카운터 + warn 으로 **관측만** 한다.
- **직렬 VL** — 동시성은 넣지 않았다(§3 스레드 안전성). 대형 스캔 문서는 1800s 초과 가능.
- **문서당 VL 상한 없음** — 사용자 결정("정확도 우선, 속도 후순위").
- **KIS 오탐은 A 로 안 고쳐진다** — `vl` 풀리플레이스 레인 삭제는 Plan B 소관.

---

## 9. 레포 상태 / 주의

- **`docker-compose.yml` 에 미커밋 변경 2건**(+17줄): gotenberg `--api-timeout=300s`(PPTX 변환 30s
  초과로 503 나던 것 수정) + `KBP_PADDLE_OCR_GATEWAY_URL` 주입(누락돼 paddle 레인이 계속 실패했음).
  **A 와 무관하지만 지우지 마라.**
- `parse_service/`·`service/` 에 다른 미커밋 변경이 있다. A 작업 전 `git status` 로 확인할 것.
- VL 개발 환경: `scripts/parse-svc.env` 에 `MODEL_API_KEY`(OpenRouter)와 `VL_MAX_TOKENS=8000`.
  **컨테이너에는 `VL_MAX_TOKENS` 가 없어 기본 2000** — §A7 에서 주입한다.
- 최종 배포는 **폐쇄망 자체 서빙**이다. OpenRouter 관련 항목(`provider.ignore`, `reasoning`)은
  거기서 무효 필드다.

---

## 10. 이어서 할 일

1. **[진행 중] Plan A v8 ultracode 검증** — 결과는 §11. READY 면 바로 구현 착수.
2. **§V3-pre 실 게이트웨이 응답 덤프** (구현 전 필수 — §7 참조)
3. **구현** §A0 → §A2 → §A3 → §A5 → §A4 → §A6 순서 권장(의존 순).
4. **검증** V1~V7.
5. **문서 반영** — `_workspace/01-architecture.md`(:58·:63·:64 MinerU 잔존 서술 제거),
   `02-changes.md`(R1~R11 요약), `03-dev-progress.md`, `README.md` 인덱스.
6. **Plan B** — A 안착 후 `planB-pdf-page-level-routing.md` 재작성부터. **v22 배관 설계를 그대로 쓰지
   마라**(미해결 blocking 4건이 문서에 기록돼 있다).

---

## 11. 검증 상태 — **현재 plan 은 v10**

### 이력 (수렴하고 있다)
| 버전 | 결과 |
|---|---|
| v19~v22 (A+B 합본) | 4라운드 연속 blocking 8건 — 전부 B 배관 → **A/B 분리 결정** |
| A v1 | blocking 4 (전부 A 자체 로직 — 분리 효과 확인) |
| A v2 | blocking 4 |
| A v3 | 편집 잔재 다수 + 임계 근거 1 |
| A v4 | **임계 상향이 오류**(정의서 p5 15.2% 탈락) → 0.05 복귀 |
| A v6 | **codebase-grounding 렌즈 READY**, blocking 2 |
| A v7 | blocking 4 (편집 잔재 3 + 실질 1) |
| A v8 | **grounding READY 재확인, blocking 1** — `KBP_VL_PAGE_DPI=200` 근거 미기재 |
| A v9 | **grounding READY, blocking 1** — 절단 판정이 ```json 펜스 형태를 놓침 |
| **A v10** | 위 blocking + minor 8건 반영. **미검증** (아래 참조) |

### v8 blocking 1건은 v9 에서 해소됨
`KBP_VL_PAGE_DPI=200` 이 근거 없는 기본값처럼 보였는데, 측정 스크립트를 확인하니 **R2·R3·R4·R6·R7·R11
이 전부 `dpi=200`** 이었다. 설계 오류가 아니라 문서 누락이라 실측 근거 절 머리에 측정 조건을 명시했다.

### v9→v10: blocking 1건 + 실측 서술 오류 정정

**(blocking) 절단 판정이 ```json 펜스를 놓쳤다.** `elements_parser.py:37` 은 파싱 전에 펜스를 벗기지만
**실패 시 fallback 에는 원문(펜스 포함)을 넣는다**(:92-102). v9 가 오탐을 줄이려 `"```" 로 시작` 규칙을
지운 탓에 가장 흔한 절단 형태가 판정 밖으로 나갔다 → paddle 본문이 raw JSON 조각으로 교체되고
`truncated=0` 이라 §V5 가 거짓 초록불. **펜스를 벗긴 뒤 `{` + `json.loads` 실패**로 고쳤다(유효 JSON
코드블록 페이지는 파싱에 성공하므로 오탐도 없다).

**(중요) 면적 임계 근거 서술이 틀렸다.** 덤프에서 페이지별 최대 visual 면적을 전수 계산했다:
```
참양성 15건   75.1 … 15.2 13.6 7.4 **5.06** %
0.55 %  ABL p21 CQ흐름   ← 참양성인데 임계 아래(미포착)
0.54 %  법원통지서 p1 QR  ← 차단 대상
```
실제 공백은 **0.55%~5.06%** 이고 0.05 는 상단에 붙어 **여유가 1.01배뿐**이다. v9 까지 "공백 0.5~7%,
7배 여유"라고 적은 건 사실이 아니었다. **값은 0.05 로 유지**한다(미발동 = 현행 유지 = 회귀 아님).
**§V5 에서 5% 부근 참양성이 관측되면 0.02~0.03 으로 하향을 검토하라. 0.01 이하는 금지**(0.54% QR 통과).

**(정정) 법원통지서의 직인은 면적이 아니라 라벨이 걸렀다** — 게이트웨이가 `seal` 라벨을 따로 내고
그 라벨은 VISUAL 화이트리스트에 없다. 면적 하한이 거른 것은 QR(image 0.54%)이다. 두 방어선이 다르다.

**(기록) 측정과 프로덕션의 인코딩이 다르다** — 측정은 무손실 PNG, 프로덕션은 JPEG q90 → q85 **2중
재인코딩**. §V4 에서 수치가 다르면 인코딩 열화인지 설계 결함인지 구분해 기록할 것.

그 외 v10 minor: `vl_extra_tables` 를 4단계(element)+5단계(block) 양쪽으로, `figure`+html 인데
markdown 도 있으면 산문 함께 산출, `one()` 클로저 arity 변경 명시, `__all__` 등재,
`blockify` 지연 import 명시.

### v9 에서 고친 것 (전부 v8 의 minor)
- `vl_extra_tables` 를 'element 수' → **'drop 된 table 블록 수'**(VL 이 pipe 표로 낸 경로까지 커버)
- `truncated` 판정을 좁히고 `error_placeholder` 와 분리(§V5 오진 방지)
- compose 의 `KBP_VL_BLOCK_PROVIDERS` 를 **콜론 없는 기본값 문법**으로(빈 문자열 opt-out)
- V3-pre 에 `width/height` 좌표계 일치 확인 추가
- heading `text_level` 소실 시 **조건부 승계** 대응 명시
- `ocr_pages` 가 비어도 계측 로그는 남김
- `_strip_gateway_image_refs` 잔재 지시 제거

### Codex 가 할 일
1. **v10 은 아직 검증에 돌리지 않았다.** 재검증할지 바로 구현할지는 판단에 맡긴다 — v10 의 변경은
   blocking 1건(펜스) + 문서 정정이고, grounding 렌즈는 v8·v9 연속 READY 였다. 재검증한다면
   워크플로 스크립트:
   `~/.claude/projects/-Users-xxx-workspace-8-kb-pipeline/43e0cd85-0161-40d8-89c3-772bf211cfb3/workflows/scripts/validate-pdf-page-routing-plan-v19-wf_cc681c37-e6b.js`
   (4개 렌즈 병렬 + 종합. 재실행 시 스크립트 안의 plan 버전 문자열만 갱신하면 된다.)
2. **READY 면 §5 순서대로 구현.** NEEDS_REVISION 이면 §2 의 실패 유형을 대조하며 plan 을 고치고
   버전을 올려 재검증한다.
3. **잔여 minor 는 blocking 이 아니다** — 라인번호 드리프트가 몇 건 남아 있다(심볼·시그니처는 전부
   실측 확인됨). 구현 중 실제 코드로 대조하면 된다.

> **검증을 무한 반복하지 마라.** 이 작업은 설계 검증에만 12라운드가 걸렸고, 후반 지적은 대부분
> **편집 잔재**(설계를 바꾸면서 따라오지 못한 문장)였다. blocking 이 "문서 내 상호모순"뿐이라면
> 그것만 정리하고 구현에 들어가는 편이 낫다. 진짜 설계 결함은 §2 의 두 유형(발동하지 않는 코드 /
> 근거를 무효화하는 가드)에 해당하는 것들이다.
