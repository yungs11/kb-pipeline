"""'image/chart 검출 → 페이지 통째로 VL' 안 실측: 표 보존 여부 확인."""
import os, sys, json
sys.path.insert(0, "/Users/xxx/workspace/8.kb-pipeline")
for line in open("/Users/xxx/workspace/8.kb-pipeline/scripts/parse-svc.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v)

import pymupdf
from parse_service.parsers.ocr import ocr_elements_sync

ABL = "/Users/xxx/Downloads/동양생명_ABL_온톨로지PoC_중간보고_v0.1.pdf"
LICO = ("/private/tmp/claude-501/-Users-xxx-workspace-8-kb-pipeline/"
        "43e0cd85-0161-40d8-89c3-772bf211cfb3/scratchpad/"
        "LIFE_AISP_PM_CC_주간보고_20241217_V1.0 (2).pdf")

for label, path, pno in [("abl_p33", ABL, 33), ("lico_p3_gantt", LICO, 3)]:
    d = pymupdf.open(path)
    img = d[pno - 1].get_pixmap(dpi=200).tobytes("png")
    els = ocr_elements_sync(img, f"{label}.png")  # 기본 전사 프롬프트
    txt = "\n\n".join((e.get("content") or {}).get("markdown") or e.get("text") or "" for e in els)
    json.dump({"text": txt}, open(f"VL_{label}.json", "w"), ensure_ascii=False)
    print(f"\n########## {label} els={len(els)} chars={len(txt)} tables={txt.count('<table')}")
    print(txt[:2500])
