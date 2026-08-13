"""표본 확대: 16페이지 layout 수집."""
import json, time, os
from concurrent.futures import ThreadPoolExecutor
import pymupdf, httpx

BASE = "https://api-doc.ys-helperai.com/ocr/paddleocr_vl"
SP = os.path.dirname(os.path.abspath(__file__))
ABL = "/Users/xxx/Downloads/동양생명_ABL_온톨로지PoC_중간보고_v0.1.pdf"
DEF = "/Users/xxx/Downloads/ST_AI_DG01_자산신탁_공통_지식베이스_프로세스_정의서_V1.0.pdf"
LICO = f"{SP}/LIFE_AISP_PM_CC_주간보고_20241217_V1.0 (2).pdf"

CASES = [
    ("abl_p13_지식그래프", ABL, 13), ("abl_p16_병렬수행체계", ABL, 16),
    ("abl_p17_PoC흐름", ABL, 17), ("abl_p22_TBox트리", ABL, 22),
    ("abl_p28_전역흐름", ABL, 28), ("abl_p35_스크린샷", ABL, 35),
    ("abl_p36_성능차트", ABL, 36), ("abl_p38_KStudio", ABL, 38),
    ("abl_p4_방사형", ABL, 4), ("abl_p6_표", ABL, 6),
    ("abl_p10_표", ABL, 10), ("abl_p21_CQ흐름", ABL, 21),
    ("def_p4_표", DEF, 4), ("def_p13_표스샷", DEF, 13),
    ("def_p7_표2", DEF, 7), ("lico_p11_대형표", LICO, 11),
]


def one(label, path, pno):
    d = pymupdf.open(path)
    img = d[pno-1].get_pixmap(dpi=150).tobytes("jpg", jpg_quality=90)
    r = httpx.post(f"{BASE}/tasks", files={"file": (f"{label}.jpg", img, "image/jpeg")},
                   data={"lang": "korean", "opts": json.dumps({"use_chart_recognition": True})},
                   timeout=180)
    r.raise_for_status()
    tid = r.json()["task_id"]
    t0 = time.time()
    while time.time() - t0 < 900:
        st = httpx.get(f"{BASE}/tasks/{tid}", timeout=60).json().get("status")
        if st in ("completed", "failed", "error"):
            break
        time.sleep(3)
    res = httpx.get(f"{BASE}/tasks/{tid}/result", timeout=120).json()
    json.dump(res, open(f"{SP}/L_{label}.json", "w"), ensure_ascii=False)
    return label, res.get("status"), "layout" in res


with ThreadPoolExecutor(max_workers=4) as ex:
    for f in [ex.submit(one, *c) for c in CASES]:
        try:
            print("OK", f.result())
        except Exception as e:
            print("FAIL", type(e).__name__, e)
