"""122b vs 235b 회귀: 스캔 전사 / 표 추출 / 다이어그램 서술. 둘 다 DeepInfra·Venice 차단."""
import os, sys, json, re
sys.path.insert(0, "/Users/xxx/workspace/8.kb-pipeline")
for line in open("/Users/xxx/workspace/8.kb-pipeline/scripts/parse-svc.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v)
os.environ["KBP_VL_DISABLE_REASONING"] = "1"
os.environ["VL_MAX_TOKENS"] = "8000"

import pymupdf
from parse_service.parsers.ocr import vl_api, ocr_elements_sync, prompts

_orig = vl_api._build_payload
def _patched(b64, up, sp):
    p = _orig(b64, up, sp)
    p["provider"] = {"ignore": ["DeepInfra", "Venice"]}
    return p
vl_api._build_payload = _patched

SP = os.path.dirname(os.path.abspath(__file__))
LICO = f"{SP}/LIFE_AISP_PM_CC_주간보고_20241217_V1.0 (2).pdf"
ABL = "/Users/xxx/Downloads/동양생명_ABL_온톨로지PoC_중간보고_v0.1.pdf"
DEF = "/Users/xxx/Downloads/ST_AI_DG01_자산신탁_공통_지식베이스_프로세스_정의서_V1.0.pdf"
NOTICE = "/Users/xxx/Downloads/1. 변론기일통지서(금혜정-신한자산 외 2).pdf"
BOOK = "/Users/xxx/Downloads/석윤수(부동산_이론실무)_ocr.pdf"

# (그룹, 라벨, pdf, pno, bbox|None, diagram?)
CASES = [
    ("A.스캔전사", "변론기일통지서_p1", NOTICE, 1, None, False),
    ("A.스캔전사", "부동산이론_p49", BOOK, 49, None, False),
    ("B.표추출", "정의서_p6", DEF, 6, None, False),
    ("B.표추출", "LICO_p10_요구사항", LICO, 10, None, False),
    ("C.다이어그램", "정의서_p5_crop", DEF, 5, [206, 470, 2793, 767], True),
    ("C.다이어그램", "ABL_p14_crop", ABL, 14, [105, 246, 1875, 1002], True),
    ("C.다이어그램", "ABL_p39_전면", ABL, 39, None, True),
]

MODELS = ["qwen/qwen3.5-122b-a10b", "qwen/qwen3-vl-235b-a22b-instruct"]


def png(path, pno, bbox, dpi=200):
    d = pymupdf.open(path); pg = d[pno-1]
    clip = None
    if bbox:
        ref = pg.get_pixmap(dpi=150)
        sx, sy = pg.rect.width/ref.width, pg.rect.height/ref.height
        clip = pymupdf.Rect(bbox[0]*sx, bbox[1]*sy, bbox[2]*sx, bbox[3]*sy)
    return pg.get_pixmap(dpi=dpi, clip=clip).tobytes("png")


out = {}
for grp, label, path, pno, bbox, diag in CASES:
    img = png(path, pno, bbox)
    ov = (prompts.DIAGRAM_SYSTEM_PROMPT, prompts.DIAGRAM_USER_PROMPT) if diag else None
    for model in MODELS:
        os.environ["MODEL_NAME"] = model
        tag = model.split("/")[-1].split("-")[0] + ("122b" if "122b" in model else "235b")
        try:
            els = ocr_elements_sync(img, f"{label}.png", ov)
            txt = "\n".join((e.get("content") or {}).get("markdown")
                            or (e.get("content") or {}).get("html")
                            or e.get("text") or "" for e in els)
        except Exception as e:
            txt = f"<<FAIL {type(e).__name__}: {e}>>"
            els = []
        out[(label, tag)] = txt
        open(f"{SP}/R_{label}_{tag}.txt", "w").write(txt)
        ntab = txt.count("<table")
        print(f"[{grp}] {label:<22} {tag:<10} els={len(els):<3} {len(txt):>6}자 tables={ntab}")

json.dump({f"{k[0]}|{k[1]}": v for k, v in out.items()},
          open(f"{SP}/regression_all.json", "w"), ensure_ascii=False, indent=1)
print("\nsaved regression_all.json")
