# VL empty retry-01 결과

최초 VL empty 5건(I14, I17, I18, I37, I46)을 `run-01/page_images`에 저장된
동일 JPEG bytes로 2026-08-12 한 번씩 재호출했다. GW는 재호출하지 않았다.

## 결과

| ID | Retry output | 원본 대조 판정 | GW 대비 | `VL_BENEFICIAL` |
|---|---:|---|---|---:|
| I14 | 1,395 chars | pass, no material error | VL | true |
| I17 | 3,960 chars | pass, table-history flattening | VL | true |
| I18 | 1,887 chars | pass, minor wording errors | GW | false |
| I37 | 1,093 chars | truncated, retrieval fail | both fail | false |
| I46 | 1,591 chars | pass, identity/amounts preserved | VL | true |

- empty 재발: **0/5**
- retry 후 retrieval pass: **4/5**
- retry 후 truncated/fail: **1/5** (I37)
- retry에서 confirmed hallucination: **0/5**
- 새 `VL_BENEFICIAL`: **3/5** (I14, I17, I46)

따라서 최초 empty는 terminal failure가 아니라 run variance 성격이 강했다. 한 번의 retry로
4/5가 usable해졌고 3/5는 GW의 critical error를 안전하게 복구했다. 다만 I37처럼 empty가
truncation으로 바뀔 뿐 usable해지지 않는 경우가 있어, non-empty만으로 성공 판정하면 안 된다.

## 1회 empty-retry를 적용한 전체 16쪽 관찰

최초 VL 결과를 보존하고, 최초 empty 5건에만 이번 결과를 채택하는 가상 policy 기준:

| 항목 | 최초 run | empty 1회 retry 적용 |
|---|---:|---:|
| Retrieval pass | 8/16 | 12/16 |
| Retrieval borderline | 1/16 | 1/16 |
| Critical error | 11/16 | 7/16 |
| Empty | 5/16 | 0/16 |
| Truncated | 2/16 | 3/16 |
| Confirmed hallucination | 2/16 | 2/16 |
| `VL_BENEFICIAL` | 3/16 | 6/16 |
| VL calls | 16 | 21 |

이 수치는 convenience DEV 16쪽의 retry-aware 관찰이며 운영 성공률이
아니다. 특히 모델 run variance를 제대로 추정하려면 독립 표본과 반복 횟수를 사전등록해야 한다.

## 실행 및 감사 정보

- exact inputs: [`page_images/`](page_images/)
- input SHA-256 / config / latency: [`manifest.json`](manifest.json)
- retry parser output: [`normalized/`](normalized/)
- raw structured output: [`vl_raw/`](vl_raw/)
- original-grounded labels: [`human_review.tsv`](human_review.tsv)
- model: `qwen/qwen3.5-122b-a10b`, temperature 0.1, max tokens 8000
- concurrency: 3, submit gap: 0초
