"""프로바이더 라우팅 분포 실측 — 지정 없이 12회."""
import os, sys, base64, collections
sys.path.insert(0, "/Users/xxx/workspace/8.kb-pipeline")
for line in open("/Users/xxx/workspace/8.kb-pipeline/scripts/parse-svc.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v)
import httpx, pymupdf

ABL = "/Users/xxx/Downloads/동양생명_ABL_온톨로지PoC_중간보고_v0.1.pdf"
d = pymupdf.open(ABL); pg = d[32]
ref = pg.get_pixmap(dpi=150)
sx, sy = pg.rect.width/ref.width, pg.rect.height/ref.height
img = pg.get_pixmap(dpi=150, clip=pymupdf.Rect(1113*sx, 352*sy, 1434*sx, 740*sy)).tobytes("png")
b64 = base64.b64encode(img).decode()
URL, KEY = os.environ["MODEL_API_URL"], os.environ["MODEL_API_KEY"]

cnt = collections.Counter()
lost = collections.Counter()
for i in range(12):
    payload = {"model": "qwen/qwen3.5-122b-a10b",
               "messages": [{"role": "user", "content": [
                   {"type": "text", "text": "이 이미지에 보이는 숫자를 나열하라."},
                   {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}],
               "max_tokens": 16, "temperature": 0.1, "reasoning": {"enabled": False}}
    j = httpx.post(URL, json=payload, timeout=180,
                   headers={"Authorization": f"Bearer {KEY}"}).json()
    prov = j.get("provider", "?")
    pt = j.get("usage", {}).get("prompt_tokens", 0)
    cnt[prov] += 1
    if pt < 100:
        lost[prov] += 1
    print(f"  {i+1:>2} {prov:<14} prompt_tok={pt:<5} {'IMAGE LOST' if pt < 100 else 'ok'}")

print("\n분포:", dict(cnt))
print("이미지 유실:", dict(lost), f"= {sum(lost.values())}/12")
