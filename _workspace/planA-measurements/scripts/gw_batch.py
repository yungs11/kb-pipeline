"""paddleocr_vl layout/chart 실측 배치."""
import json, sys, time
from concurrent.futures import ThreadPoolExecutor
import pymupdf, httpx

BASE = "https://api-doc.ys-helperai.com/ocr/paddleocr_vl"
SP = "/private/tmp/claude-501/-Users-xxx-workspace-8-kb-pipeline/43e0cd85-0161-40d8-89c3-772bf211cfb3/scratchpad"
DPI = 150

LICO = f"{SP}/LIFE_AISP_PM_CC_주간보고_20241217_V1.0 (2).pdf"
DEF = "/Users/xxx/Downloads/ST_AI_DG01_자산신탁_공통_지식베이스_프로세스_정의서_V1.0.pdf"
ABL = "/Users/xxx/Downloads/동양생명_ABL_온톨로지PoC_중간보고_v0.1.pdf"

CASES = [
    ("def_p5_흐름도", DEF, 5),
    ("def_p6_흐름도+표", DEF, 6),
    ("abl_p14_아키텍처", ABL, 14),
    ("abl_p31_RAG흐름", ABL, 31),
    ("abl_p39_파싱흐름", ABL, 39),
    ("abl_p33_리랭킹차트", ABL, 33),
    ("abl_p20_간지", ABL, 20),
    ("abl_p40_불릿본문", ABL, 40),
    ("lico_p9_성능차트", LICO, 9),
    ("lico_p3_간트", LICO, 3),
]


def render(path, pno):
    d = pymupdf.open(path)
    return d[pno - 1].get_pixmap(dpi=DPI).tobytes("jpg", jpg_quality=90)


def one(label, path, pno, opts):
    img = render(path, pno)
    r = httpx.post(f"{BASE}/tasks", files={"file": (f"{label}.jpg", img, "image/jpeg")},
                   data={"lang": "korean", "opts": json.dumps(opts)}, timeout=180)
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
    return label, res


if __name__ == "__main__":
    opts = {"use_chart_recognition": True}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(one, *c, opts) for c in CASES]
        for f in futs:
            try:
                label, res = f.result()
                print(f"OK {label} status={res.get('status')} layout={'layout' in res} err={res.get('error')}")
            except Exception as e:
                print("FAIL", type(e).__name__, e)
