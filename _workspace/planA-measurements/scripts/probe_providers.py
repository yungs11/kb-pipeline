"""122b 프로바이더별 비전 지원 실측 — 이미지가 실제로 전달되는가."""
import os, sys, json, base64
sys.path.insert(0, "/Users/xxx/workspace/8.kb-pipeline")
for line in open("/Users/xxx/workspace/8.kb-pipeline/scripts/parse-svc.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v)
import httpx, pymupdf

ABL = "/Users/xxx/Downloads/동양생명_ABL_온톨로지PoC_중간보고_v0.1.pdf"
BBOX = [1113, 352, 1434, 740]
d = pymupdf.open(ABL); pg = d[32]
ref = pg.get_pixmap(dpi=150)
sx, sy = pg.rect.width/ref.width, pg.rect.height/ref.height
img = pg.get_pixmap(dpi=250, clip=pymupdf.Rect(BBOX[0]*sx, BBOX[1]*sy, BBOX[2]*sx, BBOX[3]*sy)).tobytes("png")
b64 = base64.b64encode(img).decode()

URL, KEY = os.environ["MODEL_API_URL"], os.environ["MODEL_API_KEY"]
PROBE = "이 이미지에 보이는 모든 글자를 그대로 나열하라. 이미지가 없으면 정확히 'NO_IMAGE' 라고만 답하라."
PROVIDERS = ["SiliconFlow", "Alibaba", "DeepInfra", "AtlasCloud", "Novita"]

for model in ["qwen/qwen3.5-122b-a10b"]:
    for prov in PROVIDERS:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROBE},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}],
            "max_tokens": 400, "temperature": 0.1,
            "reasoning": {"enabled": False},
            "provider": {"only": [prov], "allow_fallbacks": False},
        }
        try:
            j = httpx.post(URL, json=payload, timeout=180,
                           headers={"Authorization": f"Bearer {KEY}"}).json()
        except Exception as e:
            print(f"{prov:<14} EXC {type(e).__name__}"); continue
        if "choices" not in j:
            print(f"{prov:<14} ERR {json.dumps(j, ensure_ascii=False)[:160]}"); continue
        txt = j["choices"][0]["message"]["content"].replace("\n", " ")
        pt = j.get("usage", {}).get("prompt_tokens")
        ok = "92" in txt and "90" in txt and "93" in txt
        print(f"{prov:<14} prompt_tok={pt:<5} vision={'OK' if ok else 'FAIL'}  {txt[:130]}")
