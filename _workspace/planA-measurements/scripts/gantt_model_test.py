"""간트 페이지: qwen3.5-122b-a10b vs qwen3-vl-235b (reasoning off)."""
import os, sys, json
sys.path.insert(0, "/Users/xxx/workspace/8.kb-pipeline")
for line in open("/Users/xxx/workspace/8.kb-pipeline/scripts/parse-svc.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v)
os.environ["KBP_VL_DISABLE_REASONING"] = "1"
os.environ["VL_MAX_TOKENS"] = "8000"

import pymupdf
from parse_service.parsers.ocr import ocr_elements_sync

SP = os.path.dirname(os.path.abspath(__file__))
LICO = f"{SP}/LIFE_AISP_PM_CC_주간보고_20241217_V1.0 (2).pdf"
GANTT_BBOX = [367, 92, 1122, 1046]  # 150dpi px, chart 블록

MODELS = ["qwen/qwen3.5-122b-a10b", "qwen/qwen3-vl-235b-a22b-instruct"]


def png(pno, bbox=None, dpi=200):
    d = pymupdf.open(LICO)
    pg = d[pno - 1]
    ref = pg.get_pixmap(dpi=150)
    clip = None
    if bbox:
        sx, sy = pg.rect.width / ref.width, pg.rect.height / ref.height
        clip = pymupdf.Rect(bbox[0] * sx, bbox[1] * sy, bbox[2] * sx, bbox[3] * sy)
    return pg.get_pixmap(dpi=dpi, clip=clip).tobytes("png")


CASES = [("fullpage", None), ("ganttcrop", GANTT_BBOX)]

for model in MODELS:
    os.environ["MODEL_NAME"] = model
    for cname, bbox in CASES:
        img = png(3, bbox)
        try:
            els = ocr_elements_sync(img, f"gantt_{cname}.png")
        except Exception as e:
            print(f"\n##### {model} / {cname} FAILED {type(e).__name__}: {e}")
            continue
        txt = "\n\n".join((e.get("content") or {}).get("markdown") or e.get("text") or "" for e in els)
        tag = model.split("/")[-1]
        open(f"{SP}/G_{tag}_{cname}.txt", "w").write(txt)
        print(f"\n##### {model} / {cname}  els={len(els)} chars={len(txt)} tables={txt.count('<table')}")
        print(txt[:2200])
