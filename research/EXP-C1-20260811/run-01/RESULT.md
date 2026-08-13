# EXP-C1-20260811 결과 — 동일 이미지 GW/VL pair labeling

## 결론

**Decision: `KEEP_OBSERVATIONAL` (Evidence E2)**

convenience DEV 16쪽을 원본 이미지와 전수 대조했다. 이 조건에서
`VL_BENEFICIAL`은 3/16(I19, I26, I35)이었다. 현재 v1 hard gate가 격리한 I24는
VL도 구제하지 못했다(0/1). 따라서 production routing rule은 승격하지 않는다.

3건의 VL 효용은 하나의 단순 신호로 설명되지 않았다.

- I26, I35: GW의 짧거나 부분적인 출력에서 당사자·사건번호·문맥을 복구한 **coverage-risk**
- I19: 1,212자의 충분히 긴 GW 출력 안에서 핵심 용어 오독을 복구한 **lexical-risk**

다음 검증은 두 feature family를 분리해 사전등록해야 한다. `gw_chars < 400` 같은
길이 기준만으로는 I19를 놓치며, 이 표본에서 얻은 사후 threshold를 production에 적용할
근거도 없다.

## 실험 조건

- parser source: `/Users/xxx/workspace/8.kb-pipeline`
- git SHA: `65e2adcbbdb14355cd8a32936731e4201712deb5`
- sample: convenience DEV, 16 pages / 16 documents. 표본 선택에 사용한 legacy positional
  label join은 사후 identity mismatch로 무효화했으며 현재 human label에는 사용하지 않음
- input: 현재 parser의 `render_pdf_pages`, 150 DPI, 추가 crop/preprocess 없음
- pair fairness: 각 쌍에 동일한 JPEG bytes 사용
- GW: 현재 프로젝트 `_post_page`; 호출 간 지연 0초; 3개 batch, engine별 최대 동시성 3
- VL: 현재 프로젝트 `ocr_elements_sync`와 `page_hybrid_prompts`
- model: `qwen/qwen3.5-122b-a10b`, temperature 0.1, max tokens 8000
- review: 16/16 저장된 원본 이미지 직접 대조 및 critical field 확인

각 엔진에 전송한 원본 한 장 이미지는 [`page_images/`](page_images/)에 보존했고,
`features.tsv`의 `image_sha256`로 정확한 바이트를 추적할 수 있다. 원본·GW·VL·자동 지표를
같은 sample id로 따라갈 수 있는 대표 사례 링크는 [`examples/INDEX.md`](examples/INDEX.md)에 있다.

## Human-grounded 결과

| 항목 | 관측값 |
|---|---:|
| `VL_BENEFICIAL` | 3/16 |
| v1 quarantine | 1/16 |
| v1 quarantine에서 VL rescue | 0/1 |
| VL empty | 5/16 |
| VL truncated | 2/16 |
| VL confirmed hallucination | 2/16 |
| GW critical error | 11/16 |
| VL critical error | 11/16 |

위 분율은 작은 convenience DEV 표본의 관측값이며 corpus prevalence, 운영 오류율,
일반 성능률이 아니다.

## 정책 비교

| Policy | Retrieval pass | Borderline | Final critical error | Confirmed hallucination | Quarantine | VL calls |
|---|---:|---:|---:|---:|---:|---:|
| OCR-only | 9 | 5 | 11 | 0 | 0 | 0 |
| VL-only | 8 | 1 | 11 | 2 | 0 | 16 |
| current v1 | 9 | 5 | 10 | 0 | 1 | 0 |
| oracle selective VL | 11 | 3 | 7 | 0 | 1 | 3 |

`oracle selective VL`은 사람이 결과를 본 뒤 3개 beneficial page만 선택한 상한선이며,
실제 router가 아니다. 비용·지연을 포함한 production 후보 성능으로 인용하면 안 된다.

## 후보 신호 관찰

| Candidate | selected | beneficial selected | DEV precision | DEV recall | 해석 |
|---|---:|---:|---:|---:|---|
| v1 hard gate | 1 | 0 | 0/1 | 0/3 | VL trigger 근거 없음 |
| GW table 없음 | 8 | 3 | 3/8 | 3/3 | eligibility 수준, trigger로는 거침 |
| `gw_chars < 400` (post hoc) | 2 | 2 | 2/2 | 2/3 | coverage 후보, 독립 검증 필요 |
| `gw_chars < 500` (post hoc) | 4 | 2 | 2/4 | 2/3 | threshold 민감성과 FP 증가 확인 |

## 대표 근거

- I19: GW `사체/부인하면/없에기/엑면금액`을 VL이 원본의
  `사채/부언하면/없애기/액면금액`으로 복구했다.
- I26: GW가 피고·피항소인 B와 승계참가인 D를 누락했고 VL은 당사자와 금액을 보존했다.
- I35: GW가 사건번호 `2024가단5205920`과 문서 문맥을 누락했고 VL은 복구했다.
- I10: VL이 원본 `제4조 ③항`을 `제4조 ⑨항`으로 바꿔 confirmed hallucination으로 판정했다.
- I24: GW는 무관한 중국어 표를 만들고 VL은 머리말에서 절단되어 둘 다 실패했다.
- I49: GW는 날짜·사건번호를 보존했지만 VL은 첫 문장에서 절단됐다.

모든 판정과 근거 문구는 [`human_review.tsv`](human_review.tsv), 엔진 간 차이는
[`disagreements.tsv`](disagreements.tsv), 집계 원본은 [`policy_metrics.json`](policy_metrics.json)에 있다.

## 지연 관측

- GW: 합계 1,331.4초, p50 74.9초, 최대 165.4초
- VL: 합계 205.0초, p50 7.6초, 최대 49.8초

페이지별 GW/VL은 동시 실행됐고 세 batch로 나눴다. **GW submit gap은 0초**였다.
합계는 wall-clock 처리량이 아니며 이 작은 convenience sample로 용량 계획을 확정하지 않는다.

## 한계와 다음 실험

- DEV 16쪽 convenience sample이며 독립 validation이 아니다. 사전 `known GW strata`는
  legacy label의 positional join 오류로 무효화했다. 자세한 정정은
  [`LEGACY_LABEL_JOIN_CORRECTION.md`](LEGACY_LABEL_JOIN_CORRECTION.md)에 있다.
- 각 엔진을 페이지당 한 번 호출해 VL run variance를 측정하지 않았다.
- provider가 model/server revision을 노출하지 않아 manifest에 unavailable로 기록했다.
- human label은 단일 reviewer 전수 판독이며 별도 adjudication은 없었다.
- 다음 revision은 결과를 보기 전에 coverage-risk와 lexical-risk feature, threshold, 표본을 고정하고
  독립 VALIDATION에서 `VL_BENEFICIAL` precision/recall, residual critical error, hallucination,
  escalation 및 비용을 함께 측정한다.

## 2026-08-12 addendum — 최초 VL empty 재시도

최초 empty 5건을 저장된 동일 JPEG로 한 번씩 다시 호출했다. 5/5가 non-empty였고,
원본 대조 결과 4/5는 retrieval pass, 1/5(I37)는 truncated fail이었다. I14, I17,
I46의 3건은 새 `VL_BENEFICIAL`로 판정됐다. 최초 결과를 덮어쓰지 않은 상세 기록은
[`retries/vl-empty-retry-01/RESULT.md`](retries/vl-empty-retry-01/RESULT.md)에 있다.

이 결과는 VL empty를 terminal failure로 단정하지 말고 bounded retry 후보로 연구해야 함을
보여준다. 동시에 non-empty를 성공으로 간주해서는 안 되며 retry 결과에도 truncation/hallucination
gate와 원본 기반 검증이 필요하다.

## 재현 산출물

- [`manifest.json`](manifest.json): 코드·모델·prompt·입력 및 실행 조건
- [`sample.tsv`](sample.tsv): 입력 문서/page mapping
- [`page_images/`](page_images/): 실제 parser 전송 원본 이미지 16장
- [`gw_raw/`](gw_raw/), [`vl_raw/`](vl_raw/), [`normalized/`](normalized/): 단계별 출력
- [`features.tsv`](features.tsv), [`auto_metrics.json`](auto_metrics.json): 자동 분석 지표
- [`human_review.tsv`](human_review.tsv), [`disagreements.tsv`](disagreements.tsv): 원본 기반 판정
- [`policy_metrics.json`](policy_metrics.json): 정책 및 후보 신호 집계
