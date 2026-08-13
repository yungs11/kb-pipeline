# 합성 측정 문서

## `m2_3lane_7p.pdf` — 3레인 혼합 (2026-08-13, M2)

보유 코퍼스 28개에 **3레인이 섞인 문서가 0건**이라 합성했다.

| p | 기대 레인 | 출처 |
|---|---|---|
| 1~3 | `odl` | `AI활용을 위한_문서 표준 가이드_신한자산신탁.pdf` p3·p4·p5 (세로 디지털) |
| 4~5 | `paddle_gw` | `석윤수(부동산_이론실무)_ocr.pdf` p7·p49 (세로 스캔, hybrid 발화 대상) |
| 6~7 | `vl` | `자산신탁 온톨로지 PoC.pdf` p4·p13 (가로형) |

triage 결과가 기대와 **7/7 일치**했다. 재생성:

```python
import pymupdf
SRC = {"odl": ("…문서 표준 가이드….pdf", [3,4,5]),
       "gw":  ("…석윤수(부동산_이론실무)_ocr.pdf", [7,49]),
       "vl":  ("test_doc/자산신탁 온톨로지 PoC.pdf", [4,13])}
out = pymupdf.open()
for _, (path, pnos) in SRC.items():
    d = pymupdf.open(path)
    for p in pnos: out.insert_pdf(d, from_page=p-1, to_page=p-1)
out.save("m2_3lane_7p.pdf")
```

**⚠️ 원본은 PII 를 포함할 수 있어 커밋하지 않는다.** 이 합성본은 공개 자료 페이지만 골랐다.
