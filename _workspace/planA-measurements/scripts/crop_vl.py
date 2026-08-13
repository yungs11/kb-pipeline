"""layout bbox crop → VL 서술 실측."""
import json, os, sys
sys.path.insert(0, "/Users/xxx/workspace/8.kb-pipeline")
for line in open("/Users/xxx/workspace/8.kb-pipeline/scripts/parse-svc.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v)

import pymupdf
from parse_service.parsers.ocr import ocr_elements_sync, prompts

SP = os.path.dirname(os.path.abspath(__file__))
DEF = "/Users/xxx/Downloads/ST_AI_DG01_자산신탁_공통_지식베이스_프로세스_정의서_V1.0.pdf"
ABL = "/Users/xxx/Downloads/동양생명_ABL_온톨로지PoC_중간보고_v0.1.pdf"

# (label, pdf, pno, bbox in 150dpi px | None=full page)
CASES = [
    ("def_p5_crop", DEF, 5, [206, 470, 2793, 767]),
    ("abl_p14_crop", ABL, 14, [105, 246, 1875, 1002]),
    ("abl_p39_full", ABL, 39, None),
]


def png(path, pno, bbox):
    d = pymupdf.open(path)
    pg = d[pno - 1]
    ref = pg.get_pixmap(dpi=150)
    clip = None
    if bbox:
        sx, sy = pg.rect.width / ref.width, pg.rect.height / ref.height
        clip = pymupdf.Rect(bbox[0] * sx, bbox[1] * sy, bbox[2] * sx, bbox[3] * sy)
    return pg.get_pixmap(dpi=200, clip=clip).tobytes("png")


ov = (prompts.DIAGRAM_SYSTEM_PROMPT, prompts.DIAGRAM_USER_PROMPT)
for label, path, pno, bbox in CASES:
    img = png(path, pno, bbox)
    els = ocr_elements_sync(img, f"{label}.png", ov)
    txt = "\n".join((e.get("content") or {}).get("markdown") or e.get("text") or "" for e in els)
    print(f"\n=== {label}  els={len(els)} chars={len(txt)}\n{txt[:1400]}")
