import os, sys
sys.path.insert(0, "/Users/xxx/workspace/8.kb-pipeline")
for line in open("/Users/xxx/workspace/8.kb-pipeline/scripts/parse-svc.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); os.environ.setdefault(k, v)
os.environ["KBP_VL_DISABLE_REASONING"] = "1"; os.environ["VL_MAX_TOKENS"] = "8000"
os.environ["MODEL_NAME"] = "qwen/qwen3.5-122b-a10b"
import pymupdf
from parse_service.parsers.ocr import vl_api, ocr_elements_sync, prompts
_o = vl_api._build_payload
vl_api._build_payload = lambda b,u,s: {**_o(b,u,s), "provider": {"ignore": ["DeepInfra","Venice"]}}
ABL = "/Users/xxx/Downloads/동양생명_ABL_온톨로지PoC_중간보고_v0.1.pdf"
for label, pno in [("p22_TBox", 22), ("p4_방사형", 4)]:
    d = pymupdf.open(ABL)
    img = d[pno-1].get_pixmap(dpi=200).tobytes("png")
    els = ocr_elements_sync(img, f"{label}.png",
                            (prompts.DIAGRAM_SYSTEM_PROMPT, prompts.DIAGRAM_USER_PROMPT))
    txt = "\n".join((e.get("content") or {}).get("markdown") or e.get("text") or "" for e in els)
    print(f"\n######## {label}  {len(txt)}자\n{txt[:1500]}")
