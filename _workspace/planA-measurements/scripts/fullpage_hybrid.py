"""전면 VL '한 페이지 통째 해석' 안 실측 — 표 보존 + 그림/차트 서술 동시."""
import os, sys, re, json
sys.path.insert(0, "/Users/xxx/workspace/8.kb-pipeline")
for line in open("/Users/xxx/workspace/8.kb-pipeline/scripts/parse-svc.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); os.environ.setdefault(k, v)
os.environ["KBP_VL_DISABLE_REASONING"] = "1"
os.environ["VL_MAX_TOKENS"] = "8000"
os.environ["MODEL_NAME"] = "qwen/qwen3.5-122b-a10b"

import pymupdf
from parse_service.parsers.ocr import vl_api, ocr_elements_sync
_o = vl_api._build_payload
vl_api._build_payload = lambda b, u, s: {**_o(b, u, s), "provider": {"ignore": ["DeepInfra", "Venice"]}}

SYS = """당신은 업무 문서 페이지를 구조 그대로 디지털화하는 문서 분석가다.
두 가지 원칙을 지킨다.
1. 글자로 된 것(본문·제목·표)은 **원문 그대로 보존**한다. 요약하지 않는다.
2. 그림으로 된 것(순서도·다이어그램·차트)은 **의미를 서술**한다. 낱말만 나열하지 않는다.
이미지에 없는 내용은 절대 지어내지 않는다.

출력은 오직 유효한 JSON. JSON 밖의 설명 금지.

## OUTPUT FORMAT
```json
{"elements": [{"category": "text", "content": {"html": "", "markdown": "...", "text": ""}, "coordinates": [], "id": 0, "page": 1}]}
```
반드시 { 로 시작해 } 로 끝낼 것."""

USER = """이 이미지는 문서 한 페이지 전체다. 읽기 순서대로 아래 규칙에 따라 옮겨라.

## 본문·제목
- 원문 그대로 전사한다. 요약·의역 금지. 마크다운 헤딩(#, ##)으로 계층을 표현한다.
- 원문자(①②③ / ㉠㉡ / ㈎㈏)는 그대로 유지한다.

## 표
- **반드시 `<table>` HTML 로 전사**한다. 한 셀도 빠뜨리지 않는다.
- 병합 셀은 `rowspan` / `colspan` 으로 정확히 표현한다.
- 표를 요약하거나 문장으로 풀어쓰는 것은 금지다.

## 순서도·다이어그램
- 박스 안 낱말만 나열하지 말고 **논리 흐름을 서술**한다.
- 시작부터 끝까지 화살표(→)로 연결하고, 조건 분기(예/아니오, True/False)는 어느 단계에서
  어디로 가는지 명시한다. 스윔레인(수행 주체)이 있으면 함께 적는다.

## 차트(막대·파이·꺾은선·간트)
- 모든 데이터 포인트를 옮기지 말고 **핵심을 3줄 이내로 요약**한다.
- 라벨과 수치가 이미지 안에서 서로 붙어 있는 것만 짝지어 쓴다. 애매하면 그 항목을 버린다.
- 하나의 수치를 두 항목에 쓰지 않는다.

## 사진·로고·장식
- 의미 없는 장식은 생략한다.

전체를 하나의 markdown 문자열로 합쳐 category="text" 로 출력하라. Output JSON now:"""

SP = os.path.dirname(os.path.abspath(__file__))
LICO = f"{SP}/LIFE_AISP_PM_CC_주간보고_20241217_V1.0 (2).pdf"
ABL = "/Users/xxx/Downloads/동양생명_ABL_온톨로지PoC_중간보고_v0.1.pdf"
DEF = "/Users/xxx/Downloads/ST_AI_DG01_자산신탁_공통_지식베이스_프로세스_정의서_V1.0.pdf"

CASES = [
    ("ABL_p33_그림+차트3+표1", ABL, 33),
    ("ABL_p17_혼합형", ABL, 17),
    ("LICO_p3_차트3", LICO, 3),
    ("ABL_p39_부서진순서도", ABL, 39),
    ("LICO_p10_표만(대조)", LICO, 10),
    ("def_p6_표2(대조)", DEF, 6),
]

for label, path, pno in CASES:
    d = pymupdf.open(path)
    img = d[pno-1].get_pixmap(dpi=200).tobytes("png")
    els = ocr_elements_sync(img, f"{label}.png", (SYS, USER))
    txt = "\n".join((e.get("content") or {}).get("markdown")
                    or (e.get("content") or {}).get("html")
                    or e.get("text") or "" for e in els)
    open(f"{SP}/F_{label}.txt", "w").write(txt)
    ntd = len(re.findall(r"<t[dh][ >]", txt))
    print(f"== {label:<26} {len(txt):>6}자 tables={txt.count('<table')} cells={ntd} "
          f"rowspan={txt.count('rowspan')} colspan={txt.count('colspan')}")
