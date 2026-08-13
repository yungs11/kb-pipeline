"""qwen3.5-9b 회귀 — A 의 근거(R3·R4·R5·R6·R7)를 그대로 재현할 수 있는가."""
import os, sys, json, base64, re
sys.path.insert(0, "/Users/xxx/workspace/8.kb-pipeline")
for line in open("/Users/xxx/workspace/8.kb-pipeline/scripts/parse-svc.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); os.environ.setdefault(k, v)
os.environ["KBP_VL_DISABLE_REASONING"] = "1"
os.environ["VL_MAX_TOKENS"] = "8000"

import httpx, pymupdf
from parse_service.parsers.ocr import vl_api, ocr_elements_sync, prompts

BLOCK = ["DeepInfra", "Venice"]
_o = vl_api._build_payload
vl_api._build_payload = lambda b, u, s: {**_o(b, u, s), "provider": {"ignore": BLOCK}}

MODEL = "qwen/qwen3.5-9b"
SP = os.path.dirname(os.path.abspath(__file__))
LICO = f"{SP}/LIFE_AISP_PM_CC_주간보고_20241217_V1.0 (2).pdf"
ABL = "/Users/xxx/Downloads/동양생명_ABL_온톨로지PoC_중간보고_v0.1.pdf"
DEF = "/Users/xxx/Downloads/ST_AI_DG01_자산신탁_공통_지식베이스_프로세스_정의서_V1.0.pdf"
NOTICE = "/Users/xxx/Downloads/1. 변론기일통지서(금혜정-신한자산 외 2).pdf"

HYBRID_EXTRA = """

## 추가 규칙 — 그림 영역의 처리 (category="figure" 인 경우)
1. 순서도·다이어그램·아키텍처도: 박스 안 낱말 나열 금지, **논리 흐름 서술**(START→END, 조건분기, 스윔레인).
2. 차트(막대·파이·꺾은선·간트): 전량 전사 금지, **핵심 3줄 이내 요약**. 라벨과 수치가 이미지 안에서
   붙어 있는 것만 짝짓고, 한 수치를 두 항목에 쓰지 않으며, 범례·축 눈금을 데이터로 오인하지 않는다.
3. 일반 본문·제목: 원문 그대로 전사.
4. 의미 없는 사진·로고·장식: 생략.
이미지에 없는 내용은 절대 지어내지 않는다."""

PAGE_HYBRID = (prompts.build_system_prompt(), prompts.build_user_prompt() + HYBRID_EXTRA)
DIAG = (prompts.DIAGRAM_SYSTEM_PROMPT, prompts.DIAGRAM_USER_PROMPT)

CASES = [
    ("R6_LICO_p10_표", LICO, 10, None, PAGE_HYBRID),
    ("R6_def_p6_표2", DEF, 6, None, PAGE_HYBRID),
    ("R5_ABL_p33_표+차트", ABL, 33, None, PAGE_HYBRID),
    ("R4_LICO_p3_간트", LICO, 3, None, PAGE_HYBRID),
    ("R3_ABL_p17_혼합", ABL, 17, None, PAGE_HYBRID),
    ("R7_법원통지서_스캔전사", NOTICE, 1, None, None),
    ("R2_ABL_p39_순서도", ABL, 39, None, DIAG),
]


def png(path, pno, dpi=200):
    d = pymupdf.open(path)
    return d[pno - 1].get_pixmap(dpi=dpi).tobytes("png")


# 0) 프로바이더 비전 확인
img = png(ABL, 33)
b64 = base64.b64encode(img).decode()
j = httpx.post(os.environ["MODEL_API_URL"], timeout=180,
               headers={"Authorization": f"Bearer {os.environ['MODEL_API_KEY']}"},
               json={"model": MODEL, "messages": [{"role": "user", "content": [
                   {"type": "text", "text": "이 이미지에 보이는 숫자를 나열하라. 이미지가 없으면 'NO_IMAGE'."},
                   {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}],
                     "max_tokens": 60, "temperature": 0.1, "reasoning": {"enabled": False},
                     "provider": {"ignore": BLOCK}}).json()
if "choices" in j:
    print(f"[비전확인] provider={j.get('provider')} prompt_tok={j.get('usage',{}).get('prompt_tokens')} "
          f":: {j['choices'][0]['message']['content'][:90]}")
else:
    print("[비전확인] ERR", json.dumps(j, ensure_ascii=False)[:200])

os.environ["MODEL_NAME"] = MODEL
print()
for label, path, pno, _, ov in CASES:
    try:
        els = ocr_elements_sync(png(path, pno), f"{label}.png", ov)
        parts = []
        for e in els:
            c = e.get("content") or {}
            parts.append(c.get("html") or c.get("markdown") or e.get("text") or "")
        txt = "\n\n".join(parts)
    except Exception as e:
        print(f"== {label:<22} FAIL {type(e).__name__}: {e}"); continue
    open(f"{SP}/Q9_{label}.txt", "w").write(txt)
    cells = len(re.findall(r"<t[dh][ >]", txt))
    pipes = len([l for l in txt.split("\n") if re.match(r"^\s*\|.*\|\s*$", l)])
    print(f"== {label:<22} els={len(els):<3} {len(txt):>6}자 tables={txt.count('<table')} "
          f"cells={cells:<4} rowspan={txt.count('rowspan')} colspan={txt.count('colspan')} pipe행={pipes}")
