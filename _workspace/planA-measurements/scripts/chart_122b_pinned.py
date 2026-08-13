"""122b + DeepInfra 배제. 프롬프트 v1(2줄) vs v2(페르소나 3줄) 비교."""
import os, sys, json, base64
sys.path.insert(0, "/Users/xxx/workspace/8.kb-pipeline")
for line in open("/Users/xxx/workspace/8.kb-pipeline/scripts/parse-svc.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v)
import httpx, pymupdf

URL, KEY = os.environ["MODEL_API_URL"], os.environ["MODEL_API_KEY"]
MODEL = "qwen/qwen3.5-122b-a10b"
SP = os.path.dirname(os.path.abspath(__file__))
LICO = f"{SP}/LIFE_AISP_PM_CC_주간보고_20241217_V1.0 (2).pdf"
ABL = "/Users/xxx/Downloads/동양생명_ABL_온톨로지PoC_중간보고_v0.1.pdf"

V1_SYS = "You are a JSON converter that summarizes charts. Output ONLY valid JSON."
V1_USER = """이 이미지는 차트(막대/파이/꺾은선/간트 등)다. 모든 데이터 포인트를 표로 전사하지 말고,
**이 차트가 말하는 핵심을 2줄 이내로 요약**하라.
- 1줄째: 이 차트가 무엇을 나타내는지 + 가장 두드러진 사실 1개(최대/최소/1위 등)를 수치와 함께.
- 2줄째(필요할 때만): 그 다음으로 의미 있는 사실 1개.
- 전체 항목 나열 금지. 표(<table>, | 구분) 출력 금지.
- 축 라벨·범례를 데이터 값으로 오인하지 말 것. 읽을 수 없으면 추측하지 말고 생략하라.
- 수치는 이미지에 적힌 그대로만 쓴다.
요약문만 출력하라(JSON·머리말 없이)."""

V2_SYS = """당신은 비즈니스 문서의 도표를 판독하는 데이터 시각화 분석가다.
직업적 원칙은 하나다 — **이미지에 적혀 있지 않은 것은 절대 쓰지 않는다.**
추정·보간·상식에 의한 보완은 오답이다. 확신이 없으면 그 항목을 통째로 생략하는 것이
틀린 수치를 적는 것보다 언제나 낫다."""
V2_USER = """이 이미지는 차트다(막대/파이/꺾은선/간트/타임라인 등).
모든 데이터 포인트를 옮겨 적지 말고, **이 차트의 핵심을 3줄 이내로 요약**하라.

## 판독 절차 (출력 전 반드시 수행)
1. 차트 종류와 주제를 파악한다.
2. 언급하려는 항목마다, **그 라벨 문자열과 그 수치 문자열이 이미지 안에서 서로 붙어 있는지**
   (같은 조각에 붙은 라벨, 같은 막대 끝의 값, 같은 마커에 달린 날짜) 눈으로 다시 확인한다.
   떨어져 있거나 애매하면 **그 항목을 버린다**.
3. 이미 쓴 수치를 다른 항목에 다시 쓰지 않았는지 확인한다.

## 작성 규칙
- 1줄째: 차트가 무엇을 나타내는지 + 가장 두드러진 사실 하나를 수치와 함께.
- 2~3줄째(있을 때만): 그 다음으로 의미 있는 사실.
- 전체 항목 나열 금지. `<table>`·`|` 표 금지.
- 수치는 인쇄된 문자 그대로만. 반올림·환산·계산 금지.
- **하나의 수치는 한 항목에만 쓴다.**
- 범례("진행중 70%", "완료")나 축 눈금을 데이터 값으로 오인하지 마라.
- **날짜·마일스톤은 라벨과 날짜가 같은 마커에 붙어 있을 때만 짝지어 쓴다.** 애매하면 생략.
- 라벨을 읽을 수 없으면 비슷한 단어로 바꾸지 말고 생략한다.

요약문만 출력하라(JSON·머리말 없이)."""

CASES = [
    ("간트", LICO, 3, [367, 92, 1122, 1046]),
    ("파이1", LICO, 3, [1139, 168, 1575, 595]),
    ("파이2", LICO, 3, [1149, 645, 1576, 1043]),
    ("막대A", ABL, 33, [746, 347, 1065, 743]),
    ("막대B", ABL, 33, [1113, 352, 1434, 740]),
    ("막대C", ABL, 33, [1514, 340, 1817, 738]),
]


def b64png(path, pno, bbox, dpi=250):
    d = pymupdf.open(path); pg = d[pno-1]
    ref = pg.get_pixmap(dpi=150)
    sx, sy = pg.rect.width/ref.width, pg.rect.height/ref.height
    raw = pg.get_pixmap(dpi=dpi, clip=pymupdf.Rect(
        bbox[0]*sx, bbox[1]*sy, bbox[2]*sx, bbox[3]*sy)).tobytes("png")
    return base64.b64encode(raw).decode()


def call(sysp, userp, b64):
    payload = {"model": MODEL, "messages": [
        {"role": "system", "content": sysp},
        {"role": "user", "content": [
            {"type": "text", "text": userp},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}],
        "max_tokens": 800, "temperature": 0.1, "reasoning": {"enabled": False},
        "provider": {"ignore": ["DeepInfra"]}}
    j = httpx.post(URL, json=payload, timeout=240,
                   headers={"Authorization": f"Bearer {KEY}"}).json()
    if "choices" not in j:
        return None, None, json.dumps(j, ensure_ascii=False)[:150]
    return (j["choices"][0]["message"]["content"].strip(),
            j.get("provider"), j.get("usage", {}).get("prompt_tokens"))


for label, path, pno, bbox in CASES:
    b64 = b64png(path, pno, bbox)
    print(f"\n===== {label}")
    for ver, (s, u) in [("v1", (V1_SYS, V1_USER)), ("v2", (V2_SYS, V2_USER))]:
        for run in range(2):
            txt, prov, pt = call(s, u, b64)
            if txt is None:
                print(f"  {ver} run{run+1} ERR {pt}"); continue
            flat = " ".join(l for l in txt.split("\n") if l.strip())
            print(f"  {ver} run{run+1} [{prov}/{pt}tok] {len(txt)}자 :: {flat[:250]}")
