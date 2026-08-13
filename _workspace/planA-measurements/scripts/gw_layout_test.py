"""paddleocr_vl 신규 layout/chart 필드 실측."""
import io, json, sys, time
import httpx
import pymupdf

BASE = "https://api-doc.ys-helperai.com/ocr/paddleocr_vl"
DPI = 150


def render(pdf_path, pno):
    doc = pymupdf.open(pdf_path)
    pg = doc[pno - 1]
    pix = pg.get_pixmap(dpi=DPI)
    return pix.tobytes("jpg", jpg_quality=90)


def post(img, name, opts):
    data = {"lang": "korean"}
    if opts:
        data["opts"] = json.dumps(opts)
    r = httpx.post(BASE + "/tasks", files={"file": (name, img, "image/jpeg")},
                   data=data, timeout=120)
    r.raise_for_status()
    return r.json()


def wait(tid, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = httpx.get(f"{BASE}/tasks/{tid}", timeout=60).json()
        if st.get("status") in ("completed", "failed", "error"):
            break
        time.sleep(3)
    return httpx.get(f"{BASE}/tasks/{tid}/result", timeout=120).json()


def run(label, pdf, pno, opts):
    img = render(pdf, pno)
    sub = post(img, f"{label}.jpg", opts)
    res = wait(sub["task_id"])
    out = f"/private/tmp/claude-501/-Users-xxx-workspace-8-kb-pipeline/43e0cd85-0161-40d8-89c3-772bf211cfb3/scratchpad/res_{label}.json"
    json.dump(res, open(out, "w"), ensure_ascii=False)
    print(f"=== {label} opts={opts} keys={list(res.keys())} status={res.get('status')} err={res.get('error')}")
    return res


if __name__ == "__main__":
    lico = "/private/tmp/claude-501/-Users-xxx-workspace-8-kb-pipeline/43e0cd85-0161-40d8-89c3-772bf211cfb3/scratchpad/LIFE_AISP_PM_CC_주간보고_20241217_V1.0 (2).pdf"
    r = run("probe_p10_chart", lico, 10, {"use_chart_recognition": True})
    print(json.dumps(r, ensure_ascii=False)[:1500])
