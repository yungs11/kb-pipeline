"""122b 환각 진단: OpenRouter provider / 응답 메타 확인."""
import os, sys, json, base64
sys.path.insert(0, "/Users/xxx/workspace/8.kb-pipeline")
for line in open("/Users/xxx/workspace/8.kb-pipeline/scripts/parse-svc.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v)

import httpx, pymupdf

SP = os.path.dirname(os.path.abspath(__file__))
ABL = "/Users/xxx/Downloads/동양생명_ABL_온톨로지PoC_중간보고_v0.1.pdf"
BBOX = [1113, 352, 1434, 740]  # 막대B (실제: PIXIE 92 / Qwen3-Emb 90 / luxia 93)

d = pymupdf.open(ABL)
pg = d[32]
ref = pg.get_pixmap(dpi=150)
sx, sy = pg.rect.width / ref.width, pg.rect.height / ref.height
img = pg.get_pixmap(dpi=250, clip=pymupdf.Rect(
    BBOX[0]*sx, BBOX[1]*sy, BBOX[2]*sx, BBOX[3]*sy)).tobytes("png")
b64 = base64.b64encode(img).decode()
print("png bytes", len(img), "b64", len(b64))

URL = os.environ["MODEL_API_URL"]
KEY = os.environ["MODEL_API_KEY"]
PROBE = "이 이미지에 보이는 모든 글자를 그대로 나열하라. 이미지가 없으면 정확히 'NO_IMAGE' 라고만 답하라."

for model in ["qwen/qwen3.5-122b-a10b", "qwen/qwen3-vl-235b-a22b-instruct"]:
    print(f"\n===== {model}")
    for i in range(3):
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROBE},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]}],
            "max_tokens": 500,
            "temperature": 0.1,
            "reasoning": {"enabled": False},
        }
        r = httpx.post(URL, json=payload, timeout=180,
                       headers={"Authorization": f"Bearer {KEY}"})
        j = r.json()
        if "choices" not in j:
            print(f"  run{i+1} ERR {json.dumps(j, ensure_ascii=False)[:300]}")
            continue
        txt = j["choices"][0]["message"]["content"].replace("\n", " ")
        print(f"  run{i+1} provider={j.get('provider')} usage={j.get('usage',{}).get('prompt_tokens')}tok")
        print(f"        {txt[:260]}")
