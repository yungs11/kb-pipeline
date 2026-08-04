"""chart crop → '핵심 2줄 요약' 프롬프트 실측."""
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
ABL = "/Users/xxx/Downloads/동양생명_ABL_온톨로지PoC_중간보고_v0.1.pdf"

SYS = """You are a JSON converter that summarizes charts. Output ONLY valid JSON. No explanations outside the JSON.

## OUTPUT FORMAT
```json
{"elements": [{"category": "figure", "content": {"html": "", "markdown": "<요약>", "text": ""}, "coordinates": [], "id": 0, "page": 1}]}
```
Start with { and end with }."""

USER = """이 이미지는 차트(막대/파이/꺾은선/간트 등)다. 모든 데이터 포인트를 표로 전사하지 말고,
**이 차트가 말하는 핵심을 2줄 이내로 요약**하라.

규칙:
- 1줄째: 이 차트가 무엇을 나타내는지 + 가장 두드러진 사실 1개(최대/최소/1위 등)를 수치와 함께.
- 2줄째(필요할 때만): 그 다음으로 의미 있는 사실 1개.
- 전체 항목 나열 금지. 표(<table>, | 구분) 출력 금지.
- 축 라벨·범례를 데이터 값으로 오인하지 말 것. 읽을 수 없으면 추측하지 말고 생략하라.
- 수치는 이미지에 적힌 그대로만 쓴다.

category="figure", content.markdown 에 위 요약을 담아 JSON 으로 출력하라. Output JSON now:"""

CASES = [
    ("lico_p3_간트막대", LICO, 3, [367, 92, 1122, 1046]),
    ("lico_p3_파이1", LICO, 3, [1139, 168, 1575, 595]),
    ("lico_p3_파이2", LICO, 3, [1149, 645, 1576, 1043]),
    ("abl_p33_막대A", ABL, 33, [746, 347, 1065, 743]),
    ("abl_p33_막대B", ABL, 33, [1113, 352, 1434, 740]),
    ("abl_p33_막대C", ABL, 33, [1514, 340, 1817, 738]),
]


def png(path, pno, bbox, dpi=250):
    d = pymupdf.open(path)
    pg = d[pno - 1]
    ref = pg.get_pixmap(dpi=150)
    sx, sy = pg.rect.width / ref.width, pg.rect.height / ref.height
    clip = pymupdf.Rect(bbox[0]*sx, bbox[1]*sy, bbox[2]*sx, bbox[3]*sy)
    return pg.get_pixmap(dpi=dpi, clip=clip).tobytes("png")


for model in ["qwen/qwen3-vl-235b-a22b-instruct", "qwen/qwen3.5-122b-a10b"]:
    os.environ["MODEL_NAME"] = model
    print(f"\n############ {model}")
    for label, path, pno, bbox in CASES:
        img = png(path, pno, bbox)
        try:
            els = ocr_elements_sync(img, f"{label}.png", (SYS, USER))
            txt = "\n".join((e.get("content") or {}).get("markdown") or e.get("text") or "" for e in els)
        except Exception as e:
            print(f"  -- {label}: FAIL {type(e).__name__}")
            continue
        txt = txt.strip()
        print(f"  -- {label}  chars={len(txt)} lines={len([l for l in txt.split(chr(10)) if l.strip()])} table={'<table' in txt or '|' in txt}")
        for l in txt.split("\n"):
            if l.strip():
                print("     ", l[:200])
