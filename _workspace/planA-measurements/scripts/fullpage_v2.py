"""전면 VL v2 — 기존 전사 프롬프트 + 그림/차트 조항 append."""
import os, sys, re
sys.path.insert(0, "/Users/xxx/workspace/8.kb-pipeline")
for line in open("/Users/xxx/workspace/8.kb-pipeline/scripts/parse-svc.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); os.environ.setdefault(k, v)
os.environ["KBP_VL_DISABLE_REASONING"] = "1"
os.environ["VL_MAX_TOKENS"] = "8000"
os.environ["MODEL_NAME"] = "qwen/qwen3.5-122b-a10b"

import pymupdf
from parse_service.parsers.ocr import vl_api, ocr_elements_sync, prompts
_o = vl_api._build_payload
vl_api._build_payload = lambda b, u, s: {**_o(b, u, s), "provider": {"ignore": ["DeepInfra", "Venice"]}}

# 기존 전사 규칙(표 HTML 계약)은 그대로 두고, figure 쪽에만 조항을 덧붙인다.
EXTRA = """

## 추가 규칙 — 그림 영역의 처리 (category="figure" 인 경우)

figure 안에 든 것이 무엇이냐에 따라 markdown 을 다르게 채운다. 표 규칙은 위 그대로다.

1. **순서도·다이어그램·아키텍처도**: 박스 안 낱말을 나열하지 말고 **논리 흐름을 서술**한다.
   시작부터 끝까지 화살표(→)로 연결하고, 조건 분기(예/아니오, True/False)는 어느 단계에서
   어디로 가는지 명시한다. 스윔레인(수행 주체)이 있으면 각 단계의 주체를 함께 적는다.

2. **차트(막대·파이·꺾은선·간트)**: 모든 데이터 포인트를 옮기지 말고 **핵심을 3줄 이내로 요약**한다.
   라벨과 수치가 이미지 안에서 서로 붙어 있는 것만 짝지어 쓴다. 애매하면 그 항목을 버린다.
   하나의 수치를 두 항목에 쓰지 않는다. 범례나 축 눈금을 데이터 값으로 오인하지 않는다.

3. **일반 본문·제목**: 지금까지대로 원문 그대로 전사한다.

4. **의미 없는 사진·로고·장식**: 생략한다.

어느 경우에도 이미지에 없는 내용을 지어내지 않는다."""

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
    ("ABL_p22_TBox(대조)", ABL, 22),
]

ov = (prompts.build_system_prompt(), prompts.build_user_prompt() + EXTRA)
for label, path, pno in CASES:
    d = pymupdf.open(path)
    img = d[pno-1].get_pixmap(dpi=200).tobytes("png")
    els = ocr_elements_sync(img, f"{label}.png", ov)
    parts = []
    for e in els:
        c = e.get("content") or {}
        parts.append(c.get("html") or c.get("markdown") or e.get("text") or "")
    txt = "\n\n".join(parts)
    open(f"{SP}/G2_{label}.txt", "w").write(txt)
    cells = len(re.findall(r"<t[dh][ >]", txt))
    pipes = len([l for l in txt.split("\n") if re.match(r"^\s*\|.*\|\s*$", l)])
    print(f"== {label:<24} els={len(els):<3} {len(txt):>6}자 tables={txt.count('<table')} "
          f"cells={cells:<4} rowspan={txt.count('rowspan')} colspan={txt.count('colspan')} pipe행={pipes}")
