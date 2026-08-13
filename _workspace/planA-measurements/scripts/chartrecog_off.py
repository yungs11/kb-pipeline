"""use_chart_recognition off 여도 chart 라벨이 검출되는가."""
import json, time, os
from concurrent.futures import ThreadPoolExecutor
import pymupdf, httpx
BASE = "http://15.164.81.29:18081/ocr/paddleocr_vl"   # 2026-08-13 이관(구 api-doc.ys-helperai.com)
SP=os.path.dirname(os.path.abspath(__file__))
LICO=f"{SP}/LIFE_AISP_PM_CC_주간보고_20241217_V1.0 (2).pdf"
ABL="/Users/xxx/Downloads/동양생명_ABL_온톨로지PoC_중간보고_v0.1.pdf"
CASES=[("off_lico_p3",LICO,3),("off_abl_p33",ABL,33),("off_abl_p36",ABL,36)]
def one(label,path,pno):
    d=pymupdf.open(path)
    img=d[pno-1].get_pixmap(dpi=150).tobytes("jpg",jpg_quality=90)
    r=httpx.post(f"{BASE}/tasks",files={"file":(f"{label}.jpg",img,"image/jpeg")},
                 data={"lang":"korean","opts":json.dumps({"use_chart_recognition":False})},timeout=180)
    r.raise_for_status(); tid=r.json()["task_id"]; t0=time.time()
    while time.time()-t0<900:
        if httpx.get(f"{BASE}/tasks/{tid}",timeout=60).json().get("status") in ("completed","failed","error"): break
        time.sleep(3)
    res=httpx.get(f"{BASE}/tasks/{tid}/result",timeout=120).json()
    json.dump(res,open(f"{SP}/C_{label}.json","w"),ensure_ascii=False)
    return label
with ThreadPoolExecutor(max_workers=3) as ex:
    for f in [ex.submit(one,*c) for c in CASES]:
        try: print("OK",f.result())
        except Exception as e: print("FAIL",type(e).__name__,e)
