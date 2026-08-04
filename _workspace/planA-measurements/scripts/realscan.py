"""실 스캔 페이지에서 has_visual 판정이 안전한가 — §5-B 의 실제 대상 도메인 측정."""
import json, time, os
from concurrent.futures import ThreadPoolExecutor
import pymupdf, httpx

BASE = "https://api-doc.ys-helperai.com/ocr/paddleocr_vl"
SP = os.path.dirname(os.path.abspath(__file__))
D = "/Users/xxx/Downloads"

CASES = [
    ("scan_법원통지서_p1", f"{D}/1. 변론기일통지서(금혜정-신한자산 외 2).pdf", 1),
    ("scan_부동산교재_p7", f"{D}/석윤수(부동산_이론실무)_ocr.pdf", 7),
    ("scan_부동산교재_p49", f"{D}/석윤수(부동산_이론실무)_ocr.pdf", 49),
    ("scan_페르소나_p1", f"{D}/AI페르소나만들기.pdf", 1),
    ("scan_페르소나_p2", f"{D}/AI페르소나만들기.pdf", 2),
    ("scan_페르소나_p5", f"{D}/AI페르소나만들기.pdf", 5),
    ("scan_하용호_p3", f"{D}/20260611_하용호_AI시대의전문성_인프런.pdf", 3),
    ("scan_하용호_p14", f"{D}/20260611_하용호_AI시대의전문성_인프런.pdf", 14),
    ("scan_하용호_p51", f"{D}/20260611_하용호_AI시대의전문성_인프런.pdf", 51),
    ("scan_ABL_p20", f"{D}/동양생명_ABL_온톨로지PoC_중간보고_v0.1.pdf", 20),
]


def one(label, path, pno):
    d = pymupdf.open(path)
    img = d[pno - 1].get_pixmap(dpi=150).tobytes("jpg", jpg_quality=90)
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
    json.dump(res, open(f"{SP}/S_{label}.json", "w"), ensure_ascii=False)
    return label


with ThreadPoolExecutor(max_workers=4) as ex:
    for f in [ex.submit(one, *c) for c in CASES]:
        try:
            print("OK", f.result())
        except Exception as e:
            print("FAIL", type(e).__name__, e)
