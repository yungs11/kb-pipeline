"""파이2(완료/기한경과/완료예정) 반복 호출로 모델 안정성 측정. 정답 16.15/0.00/83.85."""
import os, sys, re
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
BBOX = [1149, 645, 1576, 1043]

d = pymupdf.open(LICO)
pg = d[2]
ref = pg.get_pixmap(dpi=150)
sx, sy = pg.rect.width / ref.width, pg.rect.height / ref.height
clip = pymupdf.Rect(BBOX[0]*sx, BBOX[1]*sy, BBOX[2]*sx, BBOX[3]*sy)
img = pg.get_pixmap(dpi=250, clip=clip).tobytes("png")

for model in ["qwen/qwen3.5-122b-a10b", "qwen/qwen3-vl-235b-a22b-instruct"]:
    os.environ["MODEL_NAME"] = model
    print(f"\n===== {model}")
    for i in range(3):
        try:
            els = ocr_elements_sync(img, "pie2.png")
            txt = " ".join((e.get("content") or {}).get("markdown") or e.get("text") or "" for e in els)
        except Exception as e:
            print(f"  run{i+1} FAIL {type(e).__name__}")
            continue
        flat = re.sub(r"\s+", " ", txt)
        m = {k: re.search(rf"{k}[^0-9]{{0,12}}([\d.]+)\s*%", flat) for k in ("완료예정", "기한경과")}
        m2 = re.search(r"(?<!예정)(?<!경과)완료\s*[:|]?\s*([\d.]+)\s*%", flat)
        got = (m2.group(1) if m2 else "?", m["기한경과"].group(1) if m["기한경과"] else "?",
               m["완료예정"].group(1) if m["완료예정"] else "?")
        ok = got == ("16.15", "0.00", "83.85")
        print(f"  run{i+1} 완료={got[0]} 기한경과={got[1]} 완료예정={got[2]}  {'OK' if ok else 'MISMATCH'}  len={len(flat)}")
        if not ok:
            print("    ", flat[:220])
