"""최종안 E2E: 122b + provider 블록리스트 + v2 페르소나 프롬프트, 파이프라인 JSON 봉투 그대로."""
import os, sys
sys.path.insert(0, "/Users/xxx/workspace/8.kb-pipeline")
for line in open("/Users/xxx/workspace/8.kb-pipeline/scripts/parse-svc.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v)
os.environ["KBP_VL_DISABLE_REASONING"] = "1"
os.environ["VL_MAX_TOKENS"] = "8000"
os.environ["MODEL_NAME"] = "qwen/qwen3.5-122b-a10b"

import pymupdf
from parse_service.parsers.ocr import vl_api, ocr_elements_sync

# --- 제안 코드변경 시뮬레이션: provider 블록리스트 주입 ---
_orig = vl_api._build_payload
def _patched(b64, up, sp):
    p = _orig(b64, up, sp)
    bad = os.environ.get("KBP_VL_BLOCK_PROVIDERS", "")
    if bad:
        p["provider"] = {"ignore": [x.strip() for x in bad.split(",") if x.strip()]}
    return p
vl_api._build_payload = _patched
os.environ["KBP_VL_BLOCK_PROVIDERS"] = "DeepInfra,Venice"

CHART_SYS = """당신은 비즈니스 문서의 도표를 판독하는 데이터 시각화 분석가다.
직업적 원칙은 하나다 — **이미지에 적혀 있지 않은 것은 절대 쓰지 않는다.**
추정·보간·상식에 의한 보완은 오답이다. 확신이 없으면 그 항목을 통째로 생략하는 것이
틀린 수치를 적는 것보다 언제나 낫다.

출력은 오직 유효한 JSON. JSON 밖의 설명 금지.

## OUTPUT FORMAT
```json
{"elements": [{"category": "figure", "content": {"html": "", "markdown": "<요약>", "text": ""}, "coordinates": [], "id": 0, "page": 1}]}
```
반드시 { 로 시작해 } 로 끝낼 것."""

CHART_USER = """이 이미지는 차트다(막대/파이/꺾은선/간트/타임라인 등).
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

category="figure", content.markdown 에 요약을 담아 JSON 으로 출력하라. Output JSON now:"""

SP = os.path.dirname(os.path.abspath(__file__))
LICO = f"{SP}/LIFE_AISP_PM_CC_주간보고_20241217_V1.0 (2).pdf"
ABL = "/Users/xxx/Downloads/동양생명_ABL_온톨로지PoC_중간보고_v0.1.pdf"
CASES = [("간트", LICO, 3, [367, 92, 1122, 1046]),
         ("파이1", LICO, 3, [1139, 168, 1575, 595]),
         ("파이2", LICO, 3, [1149, 645, 1576, 1043]),
         ("막대A", ABL, 33, [746, 347, 1065, 743]),
         ("막대B", ABL, 33, [1113, 352, 1434, 740]),
         ("막대C", ABL, 33, [1514, 340, 1817, 738])]


def png(path, pno, bbox, dpi=250):
    d = pymupdf.open(path); pg = d[pno-1]
    ref = pg.get_pixmap(dpi=150)
    sx, sy = pg.rect.width/ref.width, pg.rect.height/ref.height
    return pg.get_pixmap(dpi=dpi, clip=pymupdf.Rect(
        bbox[0]*sx, bbox[1]*sy, bbox[2]*sx, bbox[3]*sy)).tobytes("png")


for label, path, pno, bbox in CASES:
    img = png(path, pno, bbox)
    els = ocr_elements_sync(img, f"{label}.png", (CHART_SYS, CHART_USER))
    txt = "\n".join((e.get("content") or {}).get("markdown") or e.get("text") or "" for e in els).strip()
    bad = "<table" in txt or "|" in txt or "json" in txt.lower()[:20]
    print(f"\n== {label}  els={len(els)} {len(txt)}자 표/JSON누출={bad}")
    print("   ", txt.replace("\n", "\n    ")[:400])
