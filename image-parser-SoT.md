# SoT.md — Scan Parsing / OCR–VL Routing Research

> **문서 성격:** Source of Truth (Living Document)  
> **목적:** 스캔 문서 파싱 파이프라인의 최종 구조를 실측 기반으로 결정하기 위한 단일 기준 문서  
> **상태:** v1 구현 완료 / 최종 구조 탐색 진행 중  
> **최종 갱신:** 2026-08-11  
>
> 이 문서는 “현재 무엇을 알고 있는가”, “무엇은 아직 모르는가”, “다음 실험을 어떻게 해야 하는가”를 분리해서 기록한다.  
> 새로운 실험 결과가 나오면 기존 결론을 덮어쓰지 말고 **근거·표본·조건·결론 변경 이력**을 함께 남긴다.

---

## 0. 한 줄 목표

**검색 가능성을 최대한 보존하면서, OCR/GW와 VL의 서로 다른 장점을 이용해 잘못된 텍스트·누락·hallucination을 최소화하고, 제한된 GPU를 실제로 가치가 있는 페이지에만 사용하는 최종 parsing pipeline을 찾는다.**

---

# 1. 문제 정의

대상 문서에는 스캔 PDF, 표, 계약서, 법률/소송 문서, 등기부 등 다양한 형식이 존재한다.

스캔 페이지는 텍스트 레이어가 없으므로 검색/RAG에 사용하려면 이미지에서 텍스트를 추출해야 한다.

현재 주요 선택지는 다음과 같다.

- **GW / OCR**
  - 상대적으로 저렴하고 빠름
  - 일반 스캔에서 충분히 usable한 경우가 많음
  - 실패 시 문자 오독, 숫자 오독, 구조 오류, 반복(loop) 등이 발생
  - 특히 위험한 형태는 **문장이 자연스럽지만 일부 숫자/단어가 틀리는 silent corruption**

- **VL**
  - GPU 비용이 큼
  - 성공 시 흐릿하거나 복잡한 페이지를 GW보다 잘 복구할 수 있음
  - 그러나 실패 시 빈 출력, 절단, 반복뿐 아니라 **그럴듯한 hallucination**을 만들 수 있음
  - 따라서 “GW보다 무조건 상위 엔진”으로 취급하지 않는다

이 프로젝트의 핵심 문제는 단순히 “어느 OCR이 정확한가”가 아니다.

> **어떤 페이지를 어떤 엔진으로 보내야 전체 시스템의 검색 품질·안전성·자원 효율이 가장 좋아지는가?**

---

# 2. 최종 구조를 미리 확정하지 않는다

현재 장기 가설은 다음과 같다.

```text
PAGE
 │
 ├─ DIGITAL
 │    └─ native / ODL
 │
 └─ SCAN
      └─ GW / OCR
           │
           ├─ HEALTHY
           │    └─ GW 채택
           │
           ├─ SOFT RISK
           │    └─ VL 후보
           │
           └─ HARD FAIL
                └─ quarantine 후보
```

그러나 이것은 **현재 가장 유력한 가설**일 뿐 최종 답이 아니다.

향후 실험에서는 반드시 다음 policy를 동일 표본에서 비교할 수 있어야 한다.

1. **OCR-only**
2. **VL-only**
3. **현재 v1: OCR + hard-fail quarantine**
4. **OCR + selective VL**
5. 필요 시 다른 후보 policy

최종 구조는 사전에 선호한 architecture가 아니라 **실측 결과로 결정한다.**

---

# 3. 현재 v1 기준선

## 3.1 v1의 역할

v1은 “정확도 판정기”가 아니다.

> **확실히 망가진 것만 격리하고, 애매한 결과는 검색 가능성을 위해 최대한 보존하는 baseline parser**

이다.

현재 scan lane의 핵심은 다음과 같다.

```text
SCAN
 ↓
GW
 ↓
page assessment
 │
 ├─ ENGINE_ERROR
 ├─ DEGEN_COLLAPSE
 ├─ CJK_CONTAM
 ├─ EMPTY / EMPTY_SKIPPED
 └─ ACCEPT_GW
```

현재 `ESCALATE_VL`은 contract에는 존재하지만 **v1 policy에서는 발화하지 않는다.**

단, 기존의 별도 diagram supplement 등 기존 VL 기능은 scan hard-fail fallback과 구분한다.

---

## 3.2 degen 처리 원칙

기존의 즉시 삭제 방식은 금지한다.

```text
기존
detect → 바로 삭제

현재
assess → decide → apply
```

현재 분류:

- **HARD**
  - 확실한 degeneration
  - 삭제/격리 근거로 사용할 수 있음
- **SOFT**
  - 정상 문서에서도 발생 가능한 반복
  - 증거와 metric만 기록
  - 절대 단독 삭제 근거로 사용하지 않음

현재 기준:

- 표 R1/R2: HARD
- 표 R3/R4: SOFT
- 텍스트 T1/T2/T3: HARD

이 원칙의 목적은 **silent deletion 방지**다.

---

## 3.3 v1에서 이미 확인된 기준선

현재 regression set 기준:

- authoritative label: `USABLE 49 / UNUSABLE 11`
- gate replay: `TP 3/11`
- current regression set에서 `observed FP 0/49`
- 즉 GW bad의 상당수는 그대로 통과한다

이 수치는 **v1의 품질 상한을 의미하지 않는다.**

특히 replay와 production의 render DPI가 달랐던 실험이 있으므로:

> **실험 조건이 다르면 해당 수치를 production 예측치로 사용하지 않는다.**

모든 성능 숫자는 반드시 다음과 함께 기록한다.

- 표본
- DPI
- 모델
- prompt/schema
- 코드 commit
- env
- 엔진 버전
- 날짜

---

# 4. 지금까지 실측으로 확정된 사실

아래는 현재까지의 **확정 사실**과 **아직 확정할 수 없는 해석**을 분리한 것이다.

## 4.1 확정: GW hard-fail → VL fallback은 현재 근거가 없다

기존 가설:

```text
GW hard fail
→ VL
→ rescue
```

실측에서는 반대 방향이 관찰됐다.

확인된 소표본에서:

- hard gate가 선택한 페이지에서는 VL rescue가 나오지 않음
- hallucination / empty / partial failure가 발생
- 반대로 VL rescue 사례는 GW가 겉보기에는 비교적 정상적인 페이지에 집중됨

따라서 현재 결론:

> **“GW가 대놓고 망가졌다”는 신호를 VL escalation trigger로 사용하지 않는다.**

이 결론은 “VL을 쓰지 않는다”가 아니다.

> **VL이 가치 있는 페이지를 고르는 trigger를 아직 찾지 못했다.**

가 정확한 표현이다.

## 4.2 확정: GW silent corruption이 존재한다

예:

```text
원본: 전환비율
GW  : 건한비율

원본: 액면금액
GW  : 엑면금액

원본: 19,800,000
GW  : 19,300,000
```

이 오류는 문장이 자연스러워 automated hard-fail signal로 탐지하기 어렵다.

현재 최종 구조 탐색의 핵심 연구 질문은:

> **“GW가 충분한 결과를 만들었지만 조용히 틀린 페이지를 어떻게 싸게 찾아낼 것인가?”**

이다.

## 4.3 확정: VL은 rescue 능력이 있다

VL이 GW의 silent corruption을 복구한 사례가 존재한다.

따라서 VL 자체를 기각하지 않는다.

다만 VL은 generative parser이므로 다음 위험을 별도로 관리한다.

- hallucination
- empty
- truncation
- loop/degen
- run variance
- 문맥상 그럴듯하지만 원본에 없는 내용 생성

## 4.4 확정: automated metric만으로 품질을 판정하면 안 된다

이미 다음 오류를 경험했다.

- API 응답 성공을 OCR 품질 성공으로 오해
- JSON validity를 semantic correctness로 오해
- output length를 quality proxy로 과대평가
- regex/집계 스크립트 오류로 truncation/body를 잘못 분류
- 다른 DPI를 비교해 engine 차이로 오해
- reviewer가 원본 typo를 OCR error로 잘못 판정
- 페이지 앞부분만 보고 전체 품질을 잘못 판정

따라서:

> **직접 원본 이미지를 보고 검증하지 않은 성능표는 최종 의사결정 근거가 될 수 없다.**

## 4.5 EXP-C1: VL-beneficial은 단일 failure signal이 아니었다

`EXP-C1-20260811`에서 현재 parser가 만든 동일한 150 DPI JPEG를 GW와 VL에 보내고,
convenience DEV 16쪽을 저장된 원본과 전수 대조했다. 최초에는 오류 유형 보강 표본으로
기록했으나, 2026-08-13 audit에서 legacy `gw_labels.tsv.idx`와 현재 sample 행 번호를 잘못
결합한 사실을 확인했다. 과거 strata/`prior GW label`은 무효이며 current doc/page의 근거로
사용하지 않는다. 현재 원본 대조 label과 parser output은 이 결합과 독립적이다.

- `VL_BENEFICIAL`: 3/16 (I19, I26, I35)
- current v1 quarantine: 1/16 (I24), 그 안의 VL rescue: 0/1
- VL empty 5/16, truncated 2/16, confirmed hallucination 2/16
- GW와 VL의 critical error는 각각 11/16

이 수치는 작은 convenience DEV의 관측값이며 운영 prevalence나 일반 성능률이 아니다.

beneficial 3건 중 I26/I35는 짧거나 부분적인 GW 출력의 **coverage-risk**였지만,
I19는 1,212자의 충분히 긴 GW 출력 안에 핵심 용어 오독이 있는 **lexical-risk**였다.
따라서 단순 output-length rule 하나로 `SOFT_RISK → VL`을 채우지 않는다. 두 feature
family를 분리해 독립 validation에서 검증한다.

실험 기록은 `EXP-C1-20260811`에 있으며 실제 전송 JPEG, SHA-256, GW/VL raw output,
feature, human review와 policy 비교를 함께 보존한다. 이 실행의 GW submit gap은 0초였다.

### 4.5.1 최초 VL empty 5건의 1회 retry

2026-08-12에 최초 empty 5건을 저장된 동일 JPEG bytes로 한 번씩 재호출했다.

- empty 재발: 0/5
- retry retrieval pass: 4/5
- retry truncated/fail: 1/5 (I37)
- retry confirmed hallucination: 0/5
- 새 `VL_BENEFICIAL`: 3/5 (I14, I17, I46)

이 convenience DEV에서는 VL empty가 강한 run variance를 보였고 bounded retry의 잠재 효용이
확인됐다. 그러나 I37은 empty에서 truncation으로 바뀌었을 뿐이므로 **non-empty 응답을
성공으로 판정하지 않는다.** retry 이후에도 empty/truncation/degen/hallucination gate가 필요하다.
이 결과는 E2 관찰이며 retry 횟수나 production policy를 확정하지 않는다.

---

# 5. 연구의 최상위 원칙

## P1. 원본 이미지가 최종 기준이다

OCR/VL output끼리 서로 비교해서 “누가 맞다”고 결정하지 않는다.

반드시 원본 이미지 또는 원문 텍스트 레이어가 있는 경우 원문과 비교한다.

```text
GW vs VL 비교만 함        → 불충분
GW / VL vs ORIGINAL 비교 → 필수
```

## P2. Human Visual Review는 선택 사항이 아니라 필수 단계다

모든 주요 실험에는 사람의 직접 검토가 포함되어야 한다.

### 초기/소규모 실험

표본이 작고 사람이 확인 가능한 경우:

> **전수 원본 대조를 기본값으로 한다.**

### 대규모 실험

전수 검토가 현실적으로 어려워지면 최소한 다음은 100% 사람이 본다.

- router가 엔진을 변경한 모든 페이지
- quarantine된 모든 페이지
- GW/VL 결과가 크게 다른 모든 페이지
- hallucination 의심 페이지
- critical field 오류 의심 페이지
- 새로운 rule이 발화한 모든 페이지

그리고 ACCEPT/정상군에서도 **사전에 정한 stratified random audit**를 반드시 수행한다.

“문제가 없어 보이는 페이지”를 전혀 보지 않으면 false negative와 silent corruption을 측정할 수 없v5다.

## P3. 평가 방법을 결과를 보기 전에 고정한다

실험 시작 전에 반드시 다음을 적는다.

- hypothesis
- sample
- label definition
- positive / negative 정의
- metric
- threshold
- 제외 기준
- human review 범위
- engine config

결과를 본 뒤 threshold를 바꿨다면 **새 experiment revision**으로 기록한다.

## P4. 동일 입력 조건에서 비교한다

엔진 비교 시:

- 같은 page image
- 같은 crop
- 같은 orientation
- 같은 DPI
- 가능한 동일 preprocessing

을 사용한다.

예:

```text
GW 150 dpi vs VL 300 dpi
```

결과는 engine comparison으로 사용하지 않는다.

## P5. “성공률” 하나로 합치지 않는다

다음은 서로 다른 failure다.

- OCR typo
- 숫자/금액 오류
- 이름/주소 오류
- omission
- 구조/reading-order 오류
- table 구조 손실
- empty
- truncation
- degen/loop
- hallucination

평균 OCR 점수 하나로 합치면 routing에 필요한 정보가 사라진다.

## P6. 작은 n을 일반화하지 않는다

항상 `x/y` 형태로 분모를 적는다.

금지:

```text
FPR = 0%
VL hallucination rate = 15%
```

권장:

```text
현재 regression set에서 observed FP = 0/49
해당 13p 실험에서 hallucination = 2/13
```

필요 시 confidence interval도 함께 기록한다.

## P7. Human reviewer도 틀릴 수 있다

원본 자체 typo와 OCR 오류를 구분한다.

Reviewer 판단이 애매한 페이지는 `BORDERLINE` 또는 `ADJUDICATION_REQUIRED`로 남긴다.

중요 실험은 가능하면:

```text
Reviewer A
Reviewer B
    ↓
불일치
    ↓
adjudication
```

구조를 사용한다.

---

# 6. Ground Truth / Human Review Label

단일 `USABLE / UNUSABLE`만으로는 최종 구조 연구에 부족하다.

앞으로 페이지마다 최소 다음 축을 독립적으로 기록한다.

## 6.1 Retrieval Usability

- `RETRIEVAL_PASS`
- `RETRIEVAL_BORDERLINE`
- `RETRIEVAL_FAIL`

질문:

> 이 결과가 검색/RAG recall 관점에서 실사용 가능한가?

## 6.2 Fidelity

- `NO_MATERIAL_ERROR`
- `MINOR_ERROR`
- `CRITICAL_ERROR`

Critical 예:

- 금액
- 날짜
- 조항 번호
- 사건번호
- 이름
- 회사명
- 주소
- 면적/수량
- 의무/권리 관계를 바꾸는 단어

## 6.3 Generative Safety

- `NO_HALLUCINATION`
- `HALLUCINATION_SUSPECT`
- `HALLUCINATION_CONFIRMED`

원본에 존재하지 않는 사람/주소/계약 내용/문장을 생성하면 `CONFIRMED`.

## 6.4 Coverage

- `COMPLETE_ENOUGH`
- `PARTIAL`
- `SEVERE_OMISSION`
- `EMPTY`
- `TRUNCATED`

## 6.5 Structure

- `STRUCTURE_OK`
- `READING_ORDER_ERROR`
- `HEADING_HIERARCHY_ERROR`
- `TABLE_ERROR`
- `MULTI_COLUMN_ERROR`
- `OTHER_STRUCTURE_ERROR`

## 6.6 Source anomaly

- `ORIGINAL_TYPO`
- `ORIGINAL_LOW_QUALITY`
- `ROTATED_RASTER`
- `LOW_RESOLUTION`
- `STAMP/SEAL`
- `HANDWRITING`
- `OTHER`

원본 문제와 parser 문제를 분리하기 위한 보조 label이다.

---

# 7. Human Review 절차

각 페이지는 가능하면 하나의 review 화면에서 아래를 함께 본다.
연구시 parser로 보낸 원본 이미지(페이지 한장)은 꼭 찍어서  기록해둘것.(사람이 원본페이지, parser, 각 단계별 분석지표를 비교하며 따라갈수있게.)
```text
[Original page image]

[GW output]

[VL output(s) - 해당 실험에서 생성된 경우]

[자동 feature / router decision]
```

단, **blind review가 필요한 실험**에서는 engine 이름이나 router decision을 숨긴다.

## 7.1 Reviewer가 반드시 확인할 항목

1. 제목/조항 hierarchy
2. 본문 누락
3. 숫자/금액/날짜
4. 이름/회사명/주소
5. 표 행/열 및 병합 구조
6. 다단 reading order
7. 원본에 없는 내용 생성 여부
8. 반복/loop
9. 페이지 후반부 품질

> 앞 200~300자만 보고 판정하지 않는다.

## 7.2 Critical field spot check

문서 유형별로 중요 필드를 정하고 육안 검증한다.

### 계약/소송

- 당사자
- 사건번호
- 금액
- 날짜
- 조항 번호
- 주소
- 면적/수량

### 등기부

- 법인명
- 목적
- 날짜
- 변경/등기 내용
- 대표/임원 관련 내용

실험마다 전체 transcription을 한 글자씩 검수할 필요는 없더라도, **critical field는 의도적으로 확인한다.**

---

# 8. Sampling 전략

## 8.1 임의의 “쉬운 페이지 100장” 금지

전체 corpus를 대표하도록 stratified sampling을 한다.

최소 층화 축 후보:

- 문서 유형
  - 계약서
  - 소송/법률
  - 등기부
  - 지침/규정
  - 양식
  - 기타
- scan quality
  - clean
  - blur
  - noise/background
  - low resolution
- layout
  - plain text
  - table
  - multi-column
  - mixed image/text
  - diagram
- orientation
  - normal
  - `/Rotate`
  - raster-sideways
- density
  - sparse
  - normal
  - dense
- character composition
  - 한글 중심
  - 한자/국한문 혼용
  - 숫자 다량
- expected difficulty
  - easy
  - ordinary
  - hard

## 8.2 표본 세트를 분리한다

같은 60페이지를 계속 보고 threshold를 조정하면 overfitting된다.

최소 다음 세트를 유지한다.

### `DEV`

- feature 아이디어 발굴
- threshold 탐색 가능

### `VALIDATION`

- DEV에서 정한 rule을 검증
- threshold 변경 금지

### `HOLDOUT / AUDIT`

- 최종 architecture 선택 직전에만 사용
- 가능한 오랫동안 열어보지 않는다

추가로 production 운영 중 발견된 실패 사례는:

### `REGRESSION`

- 재발하면 안 되는 사례
- 최소 reproduction fixture 또는 page reference 유지

로 승격한다.

---

# 9. 실험에서 반드시 저장할 artifact

모든 experiment run은 다음을 재현 가능하게 보관한다.

```text
experiment_id/
├─ manifest.json
├─ sample.tsv
├─ page_images/
├─ gw_raw/
├─ vl_raw/
├─ normalized/
├─ features.tsv
├─ auto_metrics.json
├─ human_review.tsv
├─ disagreements.tsv
├─ examples/
└─ RESULT.md
```

## 9.1 manifest 필수 필드

- experiment_id
- date
- git SHA
- model name
- model revision
- prompt version/hash
- guided JSON 여부
- temperature
- max_tokens
- render DPI
- parser version
- GW endpoint/version
- VL endpoint/version
- env snapshot
- sample source
- sample selection seed
- reviewer
- human review policy

**MODEL_NAME을 비롯한 production-critical config는 implicit default를 허용하지 않는다.**

---

# 10. 평가 지표

자동 지표와 human-grounded 지표를 분리한다.

## 10.1 Engine 자체 지표

엔진별:

- retrieval usable rate
- critical error rate
- hallucination rate
- severe omission rate
- empty rate
- truncation rate
- degen/loop rate
- table structure pass rate
- human review disagreement rate

## 10.2 Router 지표

최종 구조 연구에서 더 중요한 지표다.

### Escalation Rate

```text
VL 호출 페이지 / 전체 scan 페이지
```

GPU 사용량과 직접 연결된다.

### VL Rescue Rate

```text
GW에서는 불합격 또는 material error
AND
VL에서는 usable
/
VL로 보낸 페이지
```

### Harmful Replacement Rate

```text
GW가 더 낫거나 usable했는데
VL로 교체 후 더 나빠진 페이지
/
VL로 보낸 페이지
```

특히 hallucination은 별도 집계한다.

### Missed Rescue Opportunity

```text
VL이면 복구 가능했지만
router가 GW를 그대로 채택한 페이지
```

이 값이 **SOFT_RISK trigger 연구의 핵심 target**이다.

### Quarantine Precision

```text
실제로 unusable인 quarantine 페이지
/
quarantine 페이지
```

### Quarantine Recall

```text
quarantine해야 할 unusable 페이지 중
실제로 quarantine한 비율
```

### Residual Contamination

```text
최종 ingest 결과에 남은
material error / hallucination / severe omission
```

## 10.3 Resource 지표

- pages/sec
- p50 / p95 latency
- GPU sec/page
- VL calls / 1,000 pages
- gateway failure rate
- retry count
- worker crash
- peak VRAM
- estimated total processing time

최종 architecture는 accuracy만으로 선택하지 않는다.

---

# 11. 최종 비교는 “엔진 성능”이 아니라 “Policy 성능”으로 한다

예:

| Policy | Human usable | Critical error | Hallucination | Quarantine | VL calls | 처리비용 |
|---|---:|---:|---:|---:|---:|---:|
| OCR only | | | 0 | 0 | 0 | |
| VL only | | | | | 100% | |
| v1 | | | 0* | | 0* | |
| Hybrid candidate A | | | | | | |
| Hybrid candidate B | | | | | | |

\* scan hard-fail escalation 기준. 별도 diagram VL 등은 구분 집계.

최종 결정은 “VL 정확도 > OCR 정확도” 같은 문장이 아니라:

> **동일 corpus에서 어떤 routing policy가 human-grounded 품질과 GPU 비용의 가장 좋은 operating point를 만드는가**

로 한다.

---

# 12. 다음 핵심 연구 질문

## Q1. SOFT_RISK를 찾을 수 있는가?

우리가 원하는 것은:

```text
GW catastrophic failure
```

가 아니라:

```text
GW output은 멀쩡해 보임
BUT
원본과 비교하면 중요한 오독이 있음
AND
VL에서는 rescue 가능
```

인 페이지다.

이 페이지를 자동으로 찾는 cheap signal을 탐색한다.

## Q2. 어떤 feature가 “GW 오류”가 아니라 “VL 기대효용”을 예측하는가?

target을 잘못 잡지 않는다.

잘못된 target:

```text
GW_UNUSABLE
```

더 나은 target:

```text
VL_BENEFICIAL
=
quality(VL) > quality(GW)
AND
VL is safe enough
```

즉 feature가 예측해야 하는 것은 **GW 실패 여부 자체가 아니라 엔진 교체의 기대효용**이다.

---

# 13. SOFT_RISK 후보 backlog

아래는 **미검증 후보**이며 production rule이 아니다.

## Candidate A — OCR confidence

가능하면:

- character confidence
- token confidence
- line confidence
- low-confidence critical token 비율

등을 사용한다.

특히 페이지 평균 confidence보다:

> **숫자/금액/이름/법률용어 주변 confidence**

가 더 가치 있을 수 있다.

**Status: NOT TESTED**

## Candidate B — lexical anomaly

예:

- 비정상 형태소
- 법률 문맥에서 매우 희귀한 문자열
- 한글 단어 내부 비정상 문자
- 숫자 패턴 이상

주의:

고유명사/주소/법률용어 때문에 false positive 위험이 크다.

**Status: NOT TESTED**

## Candidate C — second cheap OCR disagreement

두 개의 cheap/non-generative OCR이 critical token에서 다르면 VL 후보로 승격하는 방법.

```text
OCR-A ≠ OCR-B
→ uncertain
→ VL
```

단, 추가 CPU/GPU 비용을 함께 측정한다.

**Status: NOT TESTED**

## Candidate D — critical entity disagreement / validation

금액, 날짜, 사건번호, 면적 등 형식이 강한 field에서:

- checksum-like pattern
- format validity
- 문서 내 반복 일관성
- 동일 entity의 페이지 내 불일치

를 사용한다.

단순 “형식이 맞다”는 correctness를 보장하지 않는다.

**Status: NOT TESTED**

## Candidate E — image/text coverage mismatch

layout 자체를 accuracy ground truth로 쓰지 않는다.

다만 image의 text region 대비 OCR output이 비정상적으로 적은 경우 coverage anomaly로 활용 가능성을 본다.

기존 layout A/B/C feature가 기존 hard gate에 incremental value를 주지 못한 결과를 반복하지 않도록 **새 target(VL_BENEFICIAL)에 대해 별도 검증**한다.

**Status: PARTIALLY TESTED / NOT PROVEN**

## Candidate F — document-type prior

특정 문서 유형에서 GW/VL의 failure profile이 다르다면 routing prior로 사용 가능.

예:

```text
등기부 → OCR 강점
밀집 서술형 법률문서 → VL rescue 가능성 상대적으로 높음?
```

하지만 문서 유형만으로 VL을 강제하지 않는다.

**Status: NOT TESTED**

---

# 14. 실험 Template

새로운 실험은 반드시 이 template을 복사해서 시작한다.

## EXP-XXX — 제목

### 1. 질문

> 한 문장으로 무엇을 확인하려는가?

### 2. Hypothesis

```text
H0:
H1:
```

### 3. 왜 필요한가

현재 architecture에서 어떤 미해결 문제와 연결되는가?

### 4. Target

예:

```text
VL_BENEFICIAL
HALLUCINATION_RISK
GW_CRITICAL_ERROR
```

### 5. Sample

- source:
- N:
- document count:
- page count:
- strata:
- DEV / VALIDATION / HOLDOUT:
- selection method:
- random seed:

### 6. Engine Conditions

#### GW

- version:
- DPI:
- config:

#### VL

- model:
- prompt:
- temperature:
- max_tokens:
- schema:
- DPI:

### 7. Candidate Features

사전에 나열.

### 8. Threshold

결과를 보기 전에 고정.

### 9. Automated Metrics

사전에 고정.

### 10. Human Review Plan

- 전수/표본:
- reviewer:
- blind 여부:
- critical fields:
- adjudication rule:

> **이 항목이 비어 있으면 실험 시작 금지.**

### 11. Results

자동 결과와 human-grounded 결과를 분리해서 기록.

### 12. Error Analysis

최소:

- true positive 사례
- false positive 사례
- false negative 사례
- hallucination 사례
- surprising case

원본 이미지와 함께 직접 확인.

### 13. Decision

하나만 선택:

- `PROMOTE`
- `KEEP_OBSERVATIONAL`
- `DEFER`
- `REJECT`

### 14. 이유

숫자 + 대표 원본 사례를 함께 기록.

### 15. Next Action

다음 실험 또는 구현.

---

# 15. Rule을 production에 승격시키는 조건

새 feature 또는 router rule을 production에 넣기 전에 최소 다음을 충족한다.

1. DEV가 아닌 별도 validation set에서 재현
2. 모든 발화 페이지 human review
3. 정상군 stratified random audit
4. 중요한 FP/FN 원본 사례 분석
5. 동일 DPI / 동일 engine condition
6. resource impact 측정
7. 기존 regression set 통과
8. fallback / rollback 방법 존재
9. reason/metric observability 존재
10. threshold가 특정 1~2 페이지에만 맞춘 값이 아님

---

# 16. 실험에서 절대 하지 않을 것

- output length만 보고 정확도 판단
- JSON valid만 보고 성공 판정
- API 200을 quality success로 계산
- GW output만 읽고 usable label 확정
- VL output만 보고 hallucination 여부 판단
- 다른 DPI 결과를 engine comparison으로 사용
- 표본을 본 뒤 같은 표본에 threshold 최적화하고 “검증 완료”라고 표현
- 첫 몇 문장만 보고 page label 결정
- 원본 typo를 OCR error로 자동 간주
- human review 없이 classifier 성능표만으로 production 승격
- 작은 n을 퍼센트만으로 표현
- aggregation script를 manual spot-check 없이 신뢰

---

# 17. Regression Case Registry

새로운 중요한 failure가 나오면 아래 format으로 등록한다.

| ID | 유형 | Original | GW | VL | Expected | Regression |
|---|---|---|---|---|---|---|
| REG-001 | 정상 반복표 | 정상 등기부 표 | R4 발화 | - | 보존 | yes |
| REG-002 | catastrophic loop | 춘천 p115 | loop | hallucination 사례 존재 | quarantine | yes |
| REG-003 | silent numeric corruption | 금액 원본 | 잘못된 금액 | rescue 사례 | future soft-risk | candidate |
| ... | | | | | | |

실제 PII가 포함된 문서를 그대로 fixture로 commit하지 않는다.

필요 시 비식별화하되, **비식별화 후 feature 통계를 다시 측정**한다.

---

# 18. Evidence Level

결론마다 evidence level을 붙인다.

### E0 — Idea

근거 없는 가설.

### E1 — Anecdote

1~2건 사례.

### E2 — Small measured sample

소표본에서 반복 관찰.

### E3 — Validation set reproduced

별도 validation set에서 재현.

### E4 — Production shadow / batch reproduced

production-like 조건에서 반복 재현.

### E5 — Production operating evidence

실운영 데이터로 안정성 확인.

production hard rule은 원칙적으로 E3 이상을 목표로 한다.

안전 문제처럼 극단적인 경우에는 E2에서 임시 차단할 수 있으나 반드시 deferred validation을 남긴다.

---

# 19. Current Decision Register

## D-001 — GW catastrophic fail → VL 자동 fallback

**Status:** REJECTED for v1  
**Evidence:** E2

이유:

- 현재 소표본에서 hard gate가 선택한 페이지에서 rescue가 나오지 않음
- hallucination 사례 발생

재검토 조건:

- 새로운 independent trust signal 또는 다른 VL/model/prompt가 별도 validation에서 유효함을 증명

## D-002 — VL 자체 사용

**Status:** NOT REJECTED

VL rescue 사례가 있으므로 유지한다.

다만 scan hard-fail fallback이 아닌 **selective specialist** 역할을 연구한다.

## D-003 — R3/R4 단독 destructive deletion

**Status:** REJECTED

정상 등기부 반복표 false positive가 실측됨.

현재:

```text
R3/R4 → SOFT / observe only
```

## D-004 — “애매하면 보존”

**Status:** ACTIVE v1 principle

검색 recall이 핵심인 현재 목적에서 false quarantine / silent deletion을 공격적으로 늘리지 않는다.

## D-005 — Human visual review

**Status:** MANDATORY

최종 구조를 위한 모든 주요 engine/router 비교 실험에 적용한다.

## D-006 — EXP-C1 selective VL 후보 승격

**Status:** KEEP_OBSERVATIONAL  
**Evidence:** E2  
**Experiment:** `EXP-C1-20260811`

원본 전수 대조에서 VL rescue 3/16은 확인했으나, 현재 hard gate는 0/1이고 사후
`gw_chars < 400`은 2/3만 포착했다. production rule로 승격하지 않는다.

재검토 조건:

- 독립 VALIDATION에서 coverage-risk와 lexical-risk feature를 사전등록
- `VL_BENEFICIAL` precision/recall과 최종 critical error를 측정
- VL hallucination/empty/truncation, escalation, GPU 비용을 함께 평가

## D-007 — VL empty의 bounded retry

**Status:** KEEP_OBSERVATIONAL  
**Evidence:** E2  
**Experiment:** `EXP-C1-20260811/retries/vl-empty-retry-01`

동일 입력 1회 재시도에서 최초 empty 5/5가 non-empty로 바뀌고 4/5가 retrieval pass가
되었으므로 retry 후보는 유지한다. 다만 1/5는 truncated fail이고 convenience DEV 5건뿐이라
production retry 횟수와 채택 조건은 승격하지 않는다.

재검토 조건:

- 독립 표본에서 empty 재현율과 1회/2회 retry의 한계효용 측정
- non-empty 이후 truncation/degen/hallucination 검증
- 추가 VL 호출 비용과 tail latency를 포함한 policy 비교

---

# 20. 다음 권장 Experiment Sequence

## Phase A — Baseline 재정립

### EXP-A1. Production-config baseline

목적:

- 현재 v1을 실제 production과 동일한 DPI/config에서 다시 측정
- 300dpi replay 수치를 production 성능과 분리

필수:

- original visual review
- authoritative label 재사용 시 표본 mapping 검증

## Phase B — Policy baseline

### EXP-B1. 동일 표본에서 OCR-only vs VL-only vs v1 비교

목적:

- 엔진별 장단점을 같은 표본에서 직접 비교
- hybrid가 정말 필요한지 policy 수준에서 기준선 확보

Human review:

- 초기에는 가능하면 전수

## Phase C — VL beneficial dataset 구축

### EXP-C1. GW/VL pair labeling

각 페이지에:

```text
GW_quality
VL_quality
winner = GW | VL | TIE | BOTH_FAIL
```

를 human-grounded로 부여.

그리고:

```text
VL_BENEFICIAL
=
GW material error
AND
VL usable
```

dataset을 만든다.

이 dataset이 이후 router 연구의 정답지가 된다.

## Phase D — SOFT_RISK feature 탐색

### EXP-D1+

후보 feature를 하나씩 검증한다.

목표는:

```text
GW_UNUSABLE 예측
```

이 아니라:

```text
VL_BENEFICIAL 예측
```

이다.

처음에는 interpretable feature 1~3개부터 시작한다.

ML classifier는 충분한 label data가 쌓인 뒤 검토한다.

## Phase E — Candidate Router Offline Evaluation

예:

```text
GW
 ├─ hard fail → quarantine
 ├─ soft risk → VL
 └─ otherwise → GW
```

동일 holdout에서:

- final human usable
- residual critical error
- hallucination
- VL escalation
- GPU cost

를 측정한다.

## Phase F — Shadow / Limited Batch

production-like corpus에서:

- 실제 결과는 v1로 유지
- candidate router decision과 candidate output은 shadow로만 기록

사람이 변경되는 페이지를 우선 검수한다.

충분한 근거가 쌓인 뒤에만 production policy를 변경한다.

---

# 21. 최종 architecture 선택 기준

최종 구조는 다음 질문에 모두 답할 수 있어야 한다.

### 품질

- 검색 가능한 페이지가 늘었는가?
- critical error가 줄었는가?
- hallucination이 허용 가능한 수준인가?
- quarantine이 과도하지 않은가?

### 자원

- VL escalation 비율은?
- 전체 대상 페이지 기준 GPU 처리시간은?
- peak load에서 운영 가능한가?

### 안전성

- 실패가 detectable한가?
- terminal state가 명확한가?
- retry/fallback loop가 없는가?
- wrong-model/config 사고를 막는가?

### 설명 가능성

각 페이지에 대해 최소한 다음을 설명할 수 있어야 한다.

```text
왜 GW를 썼는가?
왜 VL로 보냈는가?
왜 quarantine했는가?
어떤 signal이 발화했는가?
```

### 재현성

동일 input/config에서 실험을 재실행할 수 있어야 한다.

---

# 22. 최종 성공의 정의

최종 성공은 “OCR 정확도 99%”가 아니다.

> **실제 corpus에서 검색에 필요한 정보를 최대한 보존하면서, 잘못된 사실과 hallucination을 통제하고, VL GPU를 기대효용이 있는 페이지에만 사용하며, 모든 결정이 관측·재현·감사 가능한 상태**

가 목표다.

---

# 23. 연구 진행 시 매번 확인할 체크리스트

실험 시작 전:

- [ ] 질문이 명확한가?
- [ ] target label이 routing 목적과 맞는가?
- [ ] sample이 한 문서 유형에 치우치지 않았는가?
- [ ] DEV / VALIDATION / HOLDOUT이 구분되는가?
- [ ] 같은 input/DPI로 비교하는가?
- [ ] config/model/prompt가 고정됐는가?
- [ ] human review plan이 적혀 있는가?
- [ ] critical field 정의가 있는가?

실험 후:

- [ ] 자동 metric만 보고 결론 내리지 않았는가?
- [ ] 원본 이미지를 직접 봤는가?
- [ ] TP/FP/FN 대표 사례를 눈으로 확인했는가?
- [ ] hallucination을 원본 대조했는가?
- [ ] reviewer error / original typo 가능성을 확인했는가?
- [ ] aggregation script를 manual spot-check했는가?
- [ ] 분모와 sample 조건을 함께 기록했는가?
- [ ] 결과가 기존 결론을 뒤집으면 Decision Register를 갱신했는가?
- [ ] regression case를 추가해야 하는가?
- [ ] production rule 승격 여부를 명시했는가?

---

# 24. 문서 유지 규칙

이 `SoT.md`는 결과 보고서가 아니라 **진행 중인 판단 기준**이다.

새로운 사실이 나오면:

1. `Established Facts` 갱신
2. `Decision Register` 갱신
3. 해당 experiment ID 연결
4. 반증된 가설은 삭제하지 않고 `REJECTED`로 보존
5. 숫자는 표본/조건과 함께 기록
6. production code 변경 시 해당 decision ID를 commit/PR에 연결

이렇게 유지한다.

---

# Appendix A. 현재 핵심 철학

```text
1. 애매하면 보존한다.
2. 자동 지표보다 원본 대조가 우선이다.
3. GW와 VL은 상하관계가 아니라 failure mode가 다른 엔진이다.
4. hard failure는 VL trigger라는 보장이 없다.
5. 다음 목표는 GW failure detection이 아니라 VL-beneficial routing이다.
6. engine 정확도보다 최종 policy의 human-grounded 품질을 본다.
7. 작은 표본의 퍼센트를 production truth로 일반화하지 않는다.
8. 실험은 재현 가능해야 한다.
9. 중요한 결론은 반드시 대표 원본 사례와 함께 남긴다.
10. 최종 구조는 처음부터 정하지 않고 실측으로 결정한다.
```

---

# Appendix B. 현재 잠정 최종 가설

```text
                         PAGE
                           │
                   digital / scan
                    /           \
              native/ODL        GW/OCR
                                  │
                   ┌──────────────┼──────────────┐
                   │              │              │
                HEALTHY       SOFT_RISK       HARD_FAIL
                   │              │              │
                   ▼              ▼              ▼
                  GW             VL          QUARANTINE
                                  │
                          VL HARD FAILURE?
                           /             \
                         NO               YES
                         │                 │
                         ▼                 ▼
                        VL            QUARANTINE
```

**현재 구현된 것은 HEALTHY/HARD_FAIL 쪽의 v1 baseline이다.**

가운데 `SOFT_RISK → VL`은 아직 비어 있으며, 앞으로의 실험은 이 경로를 **감이 아니라 human-grounded evidence로 채우는 것**이 핵심이다.
