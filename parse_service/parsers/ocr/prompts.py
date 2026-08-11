"""프롬프트 관리 모듈 (모듈화 구조).

VL API 호출 시 사용되는 모든 프롬프트를 모듈 단위로 관리합니다.
- 기본 역할 정의
- 규칙 모듈 (독립적으로 조합 가능)
- 재시도 시 선택적 강화
"""

import os
from typing import List, Dict, Optional, Set


# ============================================
# 프롬프트 모듈 (독립 단위)
# ============================================

# 기본 역할 (항상 포함)
PROMPT_BASE_ROLE = """You are a JSON converter. Output ONLY valid JSON. No explanations, no additional text."""

# 출력 형식
PROMPT_OUTPUT_FORMAT = """
## MANDATORY OUTPUT FORMAT
```json
{
  "elements": [
    {
      "category": "table OR figure",
      "content": {
        "html": "string",
        "markdown": "string",
        "text": ""
      },
      "coordinates": [],
      "id": 0,
      "page": 1
    }
  ]
}
```

Start output with { and end with }:"""

# 카테고리 분류 규칙
PROMPT_CLASSIFICATION = """
## CRITICAL CLASSIFICATION RULE

### IF YOU SEE A TABLE (rows and columns of data):
- category: MUST BE "table"
- content.html: MUST contain <table>...</table>
- content.markdown: MUST BE "" (empty string)

### IF YOU SEE NON-TABLE CONTENT (text, headers, lists):
- category: MUST BE "figure"
- content.html: MUST BE "" (empty string)
- content.markdown: MUST contain text WITHOUT pipes |
- content.markdown: MUST mark every heading with markdown # syntax (see HIERARCHY RULE below)"""

# 계층구조(상위-하위) 규칙 — 2026-08-07 실측 대응.
#
# 사용자 정의: "상위-하위 개념"은 세 가지를 모두 포함한다 —
#   ① 문서 헤딩 계층(장/절/조), ② 표 병합셀의 상하위(그룹 헤더가 하위 컬럼을 거느림),
#   ③ 도형/다이어그램 안의 상하위·단계 구조(ABL 온톨로지 아키텍처도 같은 포함/단계 관계).
# 세 경우 다 "읽어서 문맥구조를 이해할 수 있어야" RAG 적재 가치가 있다.
#
# 실측 근거(①): 업무문서 9건(내규/소장/결재) 33개 헤딩 포함 페이지 **전수**에서 VL 이
# 마크다운 헤딩 마커(#)를 단 한 번도 내지 않았다(ODL·GW 는 "# 자금세탁방지 업무규정",
# "## 제1장 총칙" 을 정상 보존). 원인: 결정트리가 category 를 table|figure 로만 강제해
# blockify 의 title/heading 분기(elements_to_blocks: category ∈ {title,heading} 일 때만
# text_level 부여)가 도달 불가 + figure.markdown 안에서 # 를 쓰라는 지시가 예시로만 있고
# 규칙으로 명시되지 않았다. 결과적으로 VL 경로에서는 hybrid_to_blocks 가 text_level 을
# 만들 수 없어 enriched_content 에 장·절 계층이 통째로 사라진다(RAG 청킹 시 문맥구조 손실).
# 스키마(table|figure)는 그대로 두고 markdown/html 내부 표현만 강제한다 — 계약 불변.
#
# **env 로 통째 교체 가능**(`KBP_PROMPT_HIERARCHY_RULE`) — 소스 수정 없이 튜닝한다.
_DEFAULT_PROMPT_HIERARCHY = """
## ⚠️ CRITICAL: HIERARCHY MUST SURVIVE (headings, merged cells, diagram structure)

A reader must be able to reconstruct WHAT CONTAINS WHAT from your output.
Flattening hierarchy into a plain sequence is a VIOLATION.

### 1. DOCUMENT HEADINGS → markdown # syntax (inside content.markdown)
- Document title (문서 제목, e.g. "자금세탁방지 업무규정")   → `# `   (H1)
- Chapter (장, e.g. "제1장 총칙")                          → `## `  (H2)
- Section (절/관, e.g. "제2절 고객확인")                    → `### ` (H3)
- Article (조, e.g. "제7조(보고책임자의 역할)")              → `####` (H4)
- Slide titles / boxed section labels                       → match visual rank

A line is a heading if visually distinct — larger, bold, centered, or numbered
as 장/절/조. Do NOT mark ordinary list items (1. 2. 3. / ① ② ③ / 가. 나. 다.)
that are part of running text. Keep heading text EXACTLY as printed; only add #.

✅ "markdown": "# 자금세탁방지 업무규정\\n\\n## 제1장 총칙\\n\\n#### 제1조(목적) ..."
❌ "markdown": "자금세탁방지 업무규정\\n\\n제1장 총칙\\n\\n제1조(목적) ..."   ← hierarchy LOST

### 2. MERGED TABLE CELLS → rowspan / colspan (inside content.html)
When a header cell spans several columns/rows, it is the PARENT of those cells.
Express it with colspan/rowspan — never repeat the parent label in each child
cell, and never drop it.

✅ <tr><th rowspan="2">구분</th><th colspan="3">2025년 실적</th></tr>
   <tr><th>1분기</th><th>2분기</th><th>3분기</th></tr>
❌ flattening two header rows into one, or splitting one logical table into
   several tables so the group header loses its children.

### 3. DIAGRAM / FIGURE STRUCTURE → state containment and sequence explicitly
For architecture diagrams, ontology maps, org charts, layered/stacked figures:
describe WHICH BOX CONTAINS WHICH, and WHICH STEP LEADS TO WHICH — using the
labels exactly as printed. Nested boxes are parent→child; arrows are sequence;
columns/lanes are the actor performing each step.

✅ "markdown": "## 데이터 계층\\n- 원천 데이터: (하위) 계약DB, 고객DB, 청구DB\\n
   - 적재 계층: (하위) ETL, 정제\\n\\n흐름: 원천 데이터 → 적재 계층 → 서비스 계층"
❌ listing every box label as a flat bullet list with no containment or arrows.

Never invent structure that is not visible in the image."""


def _prompt_hierarchy() -> str:
    """계층구조 규칙 — env 로 소스 수정 없이 교체 가능(호출 시점에 읽는다)."""
    return os.environ.get("KBP_PROMPT_HIERARCHY_RULE") or _DEFAULT_PROMPT_HIERARCHY

# 원문자 처리 규칙
PROMPT_CIRCLED_CHARS = """
## ⚠️ CRITICAL: CIRCLED CHARACTERS (원문자) - MUST PRESERVE EXACTLY

### CHARACTER TYPES - UNDERSTAND THE DIFFERENCE:

# 1. Numeric Circled Numbers (숫자 원문자) - Arabic numerals inside circle
①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚㉛㉜㉝㉞㉟㊱㊲㊳㊴㊵

# 2. Korean Consonant Circled (자음 원문자) - Korean consonants inside circle
㉠㉡㉢㉣㉤㉥㉦㉧㉨㉩㉪㉫㉬㉭㉮㉯㉰㉱㉲㉳㉴㉵㉶㉷㉸㉹㉺㉻

# 3. Korean Syllable Circled (가나다 원문자) - Korean syllables inside circle
㈀㈁㈂㈃㈄㈅㈆㈇㈈㈉㈊㈋㈌㈍㈎㈏㈐㈑㈒㈓

### IDENTIFICATION RULE:
- LOOK INSIDE THE CIRCLE to identify the character type
- IF circle contains Arabic number (1,2,3...) → preserve as ①②③
- IF circle contains Korean consonant (ㄱ,ㄴ,ㄷ...) → preserve as ㉠㉡㉢
- IF circle contains Korean syllable (가,나,다...) → preserve as ㈀㈁㈂ or ㈎㈏㈐

### ABSOLUTE RULES:
- NEVER convert circled characters to plain text: ① → 1, ㉠ → ㄱ, ㈎ → 나
- NEVER convert to parentheses format: ① → (1), ㉠ → (가), ㈎ → (나)
- NEVER confuse numeric with Korean: ① ≠ ㉠ ≠ ㈀
- ALWAYS output the exact Unicode character as it appears in the document

### VALIDATION:
When you see ① → Output MUST be ① (NOT ㉠, ㈀, (1), 1)
When you see ㉠ → Output MUST be ㉠ (NOT ①, (가), (1))
When you see ㈎ → Output MUST be ㈎ (NOT ②, ㉡, (나), (2))"""

# 예시 (잘못된 케이스)
PROMPT_WRONG_EXAMPLES = """
## REAL EXAMPLES - AVOID THESE MISTAKES

### ❌ WRONG EXAMPLE 1 - Table in markdown (FORBIDDEN):
```json
{
  "category": "table",
  "content": {
    "html": "",
    "markdown": "| 구분 | 내용 |\\n|-----|-----|\\n| A | B |"
  }
}
```

### ❌ WRONG EXAMPLE 2 - Duplicate content (FORBIDDEN):
```json
{
  "category": "table",
  "content": {
    "html": "<table><tr><td>A</td></tr></table>",
    "markdown": "| A | B |"
  }
}
```

### ❌ WRONG EXAMPLE 3 - Table in figure category (FORBIDDEN):
```json
{
  "category": "figure",
  "content": {
    "markdown": "| 제목 | 값 |\\n|-----|---|\\n| X | 1 |"
  }
}
```"""

# 예시 (올바른 케이스)
PROMPT_CORRECT_EXAMPLES = """
## CORRECT EXAMPLES - FOLLOW THESE

### ✅ CORRECT EXAMPLE 1 - Table:
```json
{
  "category": "table",
  "content": {
    "html": "<table><thead><tr><th>구분</th><th>내용</th></tr></thead><tbody><tr><td>A</td><td>B</td></tr></tbody></table>",
    "markdown": "",
    "text": ""
  }
}
```

### ✅ CORRECT EXAMPLE 2 - Text with heading hierarchy:
```json
{
  "category": "figure",
  "content": {
    "html": "",
    "markdown": "# 위임전결규정\\n\\n## 제2장 전결권자\\n\\n#### 제5조(전결의 원칙) 본문 내용입니다.\\n- 항목 1\\n- 항목 2",
    "text": ""
  }
}
```
Note the # prefixes: 문서제목=#, 장=##, 절=###, 조=####. Headings without #
are a VIOLATION (document hierarchy is lost)."""

# 원문자 처리 규칙 (축약 버전 — 소형 모델용)
PROMPT_CIRCLED_CHARS_COMPACT = """
## CIRCLED CHARACTERS - PRESERVE EXACTLY
- Numeric: ①②③...⑳ — NEVER convert to (1), 1, etc.
- Korean consonant: ㉠㉡㉢ — NEVER convert to ㄱ, (가), etc.
- Korean syllable: ㈀㈁㈂ / ㈎㈏㈐ — NEVER convert to 가, (나), etc.
- Output the exact Unicode character as-is."""

# 체크리스트
PROMPT_CHECKLIST = """
## FINAL CHECKLIST
Before outputting, verify:
□ If category="table" → html has <table>, markdown=""
□ If category="figure" → html="", markdown has text
□ EVERY heading in markdown starts with # (문서제목=#, 장=##, 절=###, 조=####)
□ Merged header cells use colspan/rowspan (parent→child kept, not flattened)
□ Diagrams state containment (which box holds which) and sequence (arrows)
□ NO pipe symbols | in any markdown field
□ NO duplicate content
□ ONLY ONE JSON object"""


# ============================================
# 위반 유형별 지시사항 (재시도 시 사용)
# ============================================

VIOLATION_INSTRUCTIONS = {
    "table_empty_html": """
⚠️ CRITICAL ERROR: Table with empty HTML

If category="table", you MUST provide complete HTML table structure:
<table>
  <thead><tr><th>Header1</th><th>Header2</th></tr></thead>
  <tbody><tr><td>Data1</td><td>Data2</td></tr></tbody>
</table>

Empty html field is NOT allowed for tables.
""",

    "html_tag_mismatch": """
⚠️ CRITICAL ERROR: HTML tags not properly closed

Every opening tag MUST have a closing tag:
- <table> → </table>
- <thead> → </thead>
- <tbody> → </tbody>
- <tr> → </tr>
- <td> → </td>
- <th> → </th>

Count your tags before outputting.
""",

    "incomplete_latex": """
⚠️ CRITICAL ERROR: Incomplete LaTeX expressions

Rules for LaTeX:
- Display math: $$expression$$ (BOTH $$ required)
- Inline math: $expression$ (BOTH $ required)
- NEVER leave LaTeX unclosed

Example CORRECT: "The formula $$E = mc^2$$ represents energy."
Example WRONG: "The formula $$E = mc^2" ← Missing closing $$
""",

    "incomplete_code_block": """
⚠️ CRITICAL ERROR: Incomplete code block

Every code block MUST be properly closed:
```language
code here
```

Count the number of ``` - it must be even (opening and closing).
""",

    "incomplete_image_link": """
⚠️ CRITICAL ERROR: Incomplete image/link syntax

Correct syntax:
- Image: ![alt text](url)
- Link: [text](url)

All brackets [] and parentheses () must be properly closed.
Check: every [ has ], every ( has ).
""",

    "table_split": """
⚠️ CRITICAL ERROR: Table split across multiple elements

ENTIRE table MUST be in ONE element.

WRONG (split into 2 elements):
Element 1: {"html": "<table><thead>...</thead>"}
Element 2: {"html": "<tbody>...</tbody></table>"}

CORRECT (single element):
Element 1: {"html": "<table><thead>...</thead><tbody>...</tbody></table>"}
""",

    "nested_json": """
⚠️ CRITICAL ERROR: Nested JSON structure in content fields

content.html, content.markdown, content.text MUST be plain strings.
NO nested objects or arrays allowed.

WRONG: "html": {"nested": "object"}
WRONG: "markdown": ["array", "of", "strings"]
CORRECT: "html": "<table>...</table>"
CORRECT: "markdown": "plain text string"
""",

    "duplicate_content": """
⚠️ CRITICAL ERROR: Content in both html AND markdown

You MUST use ONLY ONE field based on category:
- If category="table" → use ONLY html, markdown MUST be ""
- If category="figure" → use ONLY markdown, html MUST be ""

NEVER fill both fields.
""",

    "pipe_table": """
⚠️ CRITICAL ERROR: Pipe table syntax detected

NEVER use pipe table syntax (| column1 | column2 |) in markdown.
Tables MUST be in HTML format using <table> tags.

If you see a table, set category="table" and use html field.
""",

    "invalid_category": """
⚠️ CRITICAL ERROR: Invalid category value

category MUST be EXACTLY one of:
- "table" (for tabular data)
- "figure" (for text, images, charts)

NO other values allowed.
""",

    "missing_required_field": """
⚠️ CRITICAL ERROR: Required fields missing

Every element MUST have:
- "category" (string: "table" or "figure")
- "content" (object with html, markdown, text)
- "content.html" (string)
- "content.markdown" (string)
- "content.text" (string)

Check your JSON structure carefully.
""",
}


# ============================================
# 위반 유형 → 관련 프롬프트 모듈 매핑
# ============================================

VIOLATION_TO_MODULES = {
    "table_empty_html": ["classification", "correct_examples"],
    "html_tag_mismatch": ["classification"],
    "incomplete_latex": [],  # 특정 모듈 없음
    "incomplete_code_block": [],
    "incomplete_image_link": [],
    "table_split": ["classification"],
    "nested_json": ["output_format"],
    "duplicate_content": ["classification", "wrong_examples"],
    "pipe_table": ["classification", "wrong_examples"],
    "invalid_category": ["classification"],
    "missing_required_field": ["output_format"],
}


# ============================================
# 프롬프트 빌더 함수
# ============================================

def build_system_prompt(include_modules: Optional[List[str]] = None) -> str:
    """System Prompt를 모듈 단위로 조합.

    Args:
        include_modules: 포함할 모듈 리스트. None이면 전체 포함.
            가능한 모듈: output_format, classification, circled_chars,
                        circled_chars_compact, wrong_examples,
                        correct_examples, checklist

    Returns:
        조합된 System Prompt
    """
    # 기본: 모든 모듈 포함
    if include_modules is None:
        include_modules = [
            "output_format",
            "classification",
            "hierarchy",        # 2026-08-07: 계층(헤딩#/병합셀/도형 상하위) 강제
            "circled_chars",
            "wrong_examples",
            "correct_examples",
            "checklist"
        ]

    # 모듈 매핑
    module_map = {
        "output_format": PROMPT_OUTPUT_FORMAT,
        "classification": PROMPT_CLASSIFICATION,
        # env(KBP_PROMPT_HIERARCHY_RULE) 로 교체 가능 — 호출 시점에 읽는다.
        "hierarchy": _prompt_hierarchy(),
        "circled_chars": PROMPT_CIRCLED_CHARS,
        "circled_chars_compact": PROMPT_CIRCLED_CHARS_COMPACT,
        "wrong_examples": PROMPT_WRONG_EXAMPLES,
        "correct_examples": PROMPT_CORRECT_EXAMPLES,
        "checklist": PROMPT_CHECKLIST,
    }

    # 조합
    parts = [PROMPT_BASE_ROLE]
    for module_name in include_modules:
        if module_name in module_map:
            parts.append(module_map[module_name])

    return "\n".join(parts)


def build_user_prompt(include_decision_tree: bool = True) -> str:
    """User Prompt 생성.

    Args:
        include_decision_tree: Decision Tree 포함 여부

    Returns:
        User Prompt
    """
    base = """Convert this document to JSON. Follow these EXACT rules:"""

    if include_decision_tree:
        decision_tree = """

## DECISION TREE (MUST FOLLOW):
```
Is it a table (grid with rows/columns)?
├─ YES: category="table"
│   ├─ content.html = "<table>...</table>"
│   └─ content.markdown = "" ← MUST BE EMPTY
│
└─ NO: category="figure"
    ├─ content.html = "" ← MUST BE EMPTY
    └─ content.markdown = "text content" ← NO PIPES |
        └─ headings MUST start with # ← 문서제목=#, 장=##, 절=###, 조=####
```"""
        base += decision_tree

    violations_to_avoid = """

## EXAMPLES OF VIOLATIONS TO AVOID:

1. ❌ WRONG - Pipe table in markdown:
   "markdown": "| 제재대상 | 제재내용 |"

2. ❌ WRONG - Same table in both fields:
   "html": "<table>...</table>",
   "markdown": "| same | data |"

3. ❌ WRONG - Empty html for table category:
   "category": "table",
   "html": ""

4. ❌ WRONG - Heading without # (document hierarchy is LOST):
   "markdown": "제1장 총칙\\n\\n제1조(목적) ..."
   ✅ RIGHT: "markdown": "## 제1장 총칙\\n\\n#### 제1조(목적) ..."

## CORRECT OUTPUT:
- Tables → category="table" + HTML only
- Text → category="figure" + Markdown only
- Headings → ALWAYS prefixed with # (문서제목=#, 장=##, 절=###, 조=####)
- Merged header cells → colspan/rowspan (parent→child preserved)
- Diagrams → state containment (which box holds which) + sequence (arrows)
- NEVER mix formats
- NEVER duplicate content
- NO pipe symbols | anywhere

Output JSON now (start with { end with }):"""

    return base + violations_to_avoid


def get_relevant_modules_for_violations(violations: List[Dict[str, str]]) -> Set[str]:
    """위반 유형에서 관련 프롬프트 모듈 추출.

    Args:
        violations: 위반 사항 리스트

    Returns:
        관련 모듈명 집합
    """
    relevant_modules = set()

    for violation in violations:
        vtype = violation.get("type", "")
        if vtype in VIOLATION_TO_MODULES:
            relevant_modules.update(VIOLATION_TO_MODULES[vtype])

    return relevant_modules


def build_retry_system_prompt(violations: List[Dict[str, str]]) -> str:
    """재시도용 System Prompt 생성 (위반 관련 모듈만 포함).

    Args:
        violations: 위반 사항 리스트

    Returns:
        최적화된 System Prompt
    """
    # 항상 포함할 기본 모듈
    essential_modules = ["output_format", "classification"]

    # 위반 관련 모듈
    relevant_modules = get_relevant_modules_for_violations(violations)

    # 조합 (중복 제거)
    all_modules = list(set(essential_modules) | relevant_modules)

    return build_system_prompt(include_modules=all_modules)


def generate_retry_prompt(
    base_user_prompt: str,
    violations: List[Dict[str, str]],
    retry_count: int
) -> str:
    """검증 실패 내용을 기반으로 재시도용 강화 프롬프트 생성.

    Args:
        base_user_prompt: 기본 사용자 프롬프트
        violations: 위반 사항 리스트 [{"type": "...", "message": "..."}, ...]
        retry_count: 현재 재시도 횟수

    Returns:
        강화된 프롬프트 문자열
    """
    if not violations:
        return base_user_prompt

    # 위반 유형 추출
    violation_types = set()
    for violation in violations:
        vtype = violation.get("type", "")
        if vtype in VIOLATION_INSTRUCTIONS:
            violation_types.add(vtype)

    if not violation_types:
        return base_user_prompt

    # 추가 지시사항 조합
    enhancements = []
    for vtype in sorted(violation_types):
        enhancements.append(VIOLATION_INSTRUCTIONS[vtype])

    # 최종 프롬프트 생성
    enhanced_prompt = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️⚠️⚠️ VALIDATION FAILED - RETRY ATTEMPT {retry_count} ⚠️⚠️⚠️
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Your previous response had the following CRITICAL ERRORS:

{''.join(enhancements)}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PLEASE FIX THESE ERRORS IN YOUR NEXT RESPONSE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{base_user_prompt}
"""

    return enhanced_prompt


# ============================================
# Excel 전용 프롬프트 (차트 및 이미지 VL 처리)
# ============================================

# Excel 차트 분석용 프롬프트
EXCEL_CHART_SYSTEM_PROMPT = "당신은 차트와 그래프를 분석하는 전문가입니다."

EXCEL_CHART_USER_PROMPT = """이 차트를 상세히 설명해주세요.

포함할 내용:
- 차트 제목
- 차트 타입 (막대, 선, 원형 등)
- 표현된 데이터와 수치
- 축 정보 (있는 경우)
- 범례 정보

설명은 명확하고 구체적으로 작성해주세요."""

# Excel 이미지 OCR용 프롬프트
EXCEL_IMAGE_SYSTEM_PROMPT = "당신은 이미지에서 텍스트를 추출하는 OCR 전문가입니다."

EXCEL_IMAGE_USER_PROMPT = """이 이미지에서 모든 텍스트를 추출해주세요.

규칙:
- OCR로 인식되는 모든 텍스트
- 텍스트만 추출 (설명 불필요)
- 줄바꿈 유지
- 순서대로 추출

예시 출력:
ABC Corporation
Innovation & Excellence"""


def get_excel_prompts() -> Dict[str, Dict[str, str]]:
    """Excel VL 처리용 프롬프트 반환.

    Returns:
        {
            "chart": {"system_prompt": ..., "user_prompt": ...},
            "image": {"system_prompt": ..., "user_prompt": ...}
        }
    """
    return {
        "chart": {
            "system_prompt": EXCEL_CHART_SYSTEM_PROMPT,
            "user_prompt": EXCEL_CHART_USER_PROMPT
        },
        "image": {
            "system_prompt": EXCEL_IMAGE_SYSTEM_PROMPT,
            "user_prompt": EXCEL_IMAGE_USER_PROMPT
        }
    }


# ============================================
# 다이어그램(순서도/플로우차트) 전용 프롬프트 — 보충 호출용
# ============================================
# 범용 전사 프롬프트는 순서도를 "박스 라벨 나열"로 전사한다(흐름 유실). 다이어그램 페이지
# 보충에는 이 프롬프트로 **논리 흐름을 서술**시킨다. 출력 스키마는 동일(elements/figure/markdown)
# 이라 blockify·재분류 경로를 그대로 탄다.
DIAGRAM_SYSTEM_PROMPT = """You are a JSON converter that describes diagrams and flowcharts. Output ONLY valid JSON. No explanations outside the JSON.

## OUTPUT FORMAT
```json
{"elements": [{"category": "figure", "content": {"html": "", "markdown": "<설명>", "text": ""}, "coordinates": [], "id": 0, "page": 1}]}
```
Start with { and end with }."""

DIAGRAM_USER_PROMPT = """이 이미지는 업무 순서도/플로우차트/다이어그램이다. 박스 안 글자를 그대로 나열하지 말고, **논리 흐름을 서술**하라.

규칙:
- 시작(START)부터 끝(END)까지 각 단계를 순서대로, 화살표(→)로 연결해 흐름이 드러나게 서술한다.
- 조건 분기(예: 미납/보완, 불일치/보완, 예/아니오)가 있으면 어느 단계에서 어떤 조건으로 어디로 가는지 명시한다.
- 스윔레인(수행 주체: 현업/시스템 등)이 있으면 각 단계의 주체를 함께 적는다.
- 순서도 밖의 표·주석·체크리스트·서류 목록 텍스트는 **빠뜨리지 말고** 원문 그대로 보존한다(순서도 아닌 부분은 전사).
- 원문자(①②③ / ㉠㉡ / ㈎㈏)는 정확히 그대로 유지한다.

category="figure", content.markdown 에 위 서술을 담아 JSON 으로 출력하라. Output JSON now:"""


# ============================================
# 하위 호환성 유지
# ============================================

# 기존 코드와의 호환을 위해 전체 프롬프트 생성
SYSTEM_PROMPT = build_system_prompt()
USER_PROMPT = build_user_prompt()


def get_default_prompts() -> Dict[str, str]:
    """기본 프롬프트 반환 (하위 호환성 유지).

    Returns:
        {"system_prompt": ..., "user_prompt": ...}
    """
    return {
        "system_prompt": SYSTEM_PROMPT,
        "user_prompt": USER_PROMPT
    }


# 순서도/다이어그램 조항 — 라벨 유지 vs 흐름서술 사이에서 모델이 라벨을 "첫 번째 단계"
# 식으로 일반화해버리는 문제가 실측(2026-08-06, 가로형 합성 PDF: "접수→검토→승인" 박스가
# "첫 번째 단계→두 번째 단계→세 번째 단계"로 뭉개짐)됐다. 그래서 "라벨은 원문 그대로 보존"을
# 첫 문장에 명시하고, 화살표 연결은 그 다음이라는 순서로 강제한다.
_DEFAULT_PAGE_HYBRID_DIAGRAM_RULE = """1. **순서도·다이어그램·아키텍처도**: 박스·도형 안에 적힌 라벨(단계명·항목명)은
   **이미지에 보이는 글자 그대로** 옮긴다 — "첫 번째 단계"/"단계1"처럼 일반화하거나 순서
   번호로 바꾸지 않는다.
   그 다음 **구조를 드러낸다** — 둘 다 필요하다:
   - **상하위(포함) 관계**: 박스 안에 박스가 있거나, 계층/레이어/그룹으로 묶여 있으면
     무엇이 무엇을 포함하는지 명시한다(들여쓰기 목록 또는 "A: (하위) B, C" 형식).
     아키텍처도·온톨로지맵·조직도·레이어드 구성도가 여기 해당한다.
   - **흐름(순서) 관계**: 화살표(→)로 연결해 논리 흐름을 서술한다. 조건 분기(예/아니오,
     True/False)는 어느 라벨에서 어느 라벨로 가는지 명시한다. 스윔레인(수행 주체)이
     있으면 각 단계의 주체를 함께 적는다.
   라벨만 평평하게 나열하는 것은 **금지** — 읽는 사람이 원본 도형의 계층·순서를 복원할 수
   있어야 한다."""


def _page_hybrid_diagram_rule() -> str:
    """순서도 조항 — env 로 소스 수정 없이 조정 가능(2026-08-06). 호출 시점에 읽는다
    (다른 KBP_TRIAGE_*/KBP_GATE_* 와 동일 관례 — monkeypatch.setenv 테스트 가능)."""
    return os.environ.get("KBP_PAGE_HYBRID_DIAGRAM_RULE") or _DEFAULT_PAGE_HYBRID_DIAGRAM_RULE


def _page_hybrid_extra() -> str:
    return f"""

## 추가 규칙 — 그림 영역의 처리 (category="figure" 인 경우)

figure 안에 든 것이 무엇이냐에 따라 markdown 을 다르게 채운다. 위의 표 규칙은 그대로다.

{_page_hybrid_diagram_rule()}

2. **차트(막대·파이·꺾은선·간트)**: 모든 데이터 포인트를 옮기지 말고 **핵심을 3줄 이내로 요약**한다.
   라벨과 수치가 이미지 안에서 서로 붙어 있는 것만 짝지어 쓴다. 애매하면 그 항목을 버린다.
   하나의 수치를 두 항목에 쓰지 않는다. 범례나 축 눈금을 데이터 값으로 오인하지 않는다.

3. **일반 본문·제목**: 지금까지대로 **원문 그대로 전사**한다(요약 금지).

4. **의미 없는 사진·로고·장식**: 생략한다.

어느 경우에도 이미지에 없는 내용을 지어내지 않는다."""


def page_hybrid_prompts() -> tuple[str, str]:
    """(system, user) — 기존 전사 프롬프트 + 그림/차트 조항. **호출 시점에** env
    (`KBP_PAGE_HYBRID_DIAGRAM_RULE`)를 읽는다 — 소비자(`pdf/__init__.py`,
    `ocr/__init__.py`)는 이 함수를 매 호출마다 불러야 env 변경이 반영된다."""
    return build_system_prompt(), build_user_prompt() + _page_hybrid_extra()


# 하위 호환 별칭(구 이름 — 일부 소비자가 여전히 import 할 수 있음).
build_page_hybrid_prompts = page_hybrid_prompts

# 정적 상수(하위 호환) — **모듈 로드 시점** 값이라 이후 env 변경을 반영하지 않는다.
# 실제 소비자는 반드시 `page_hybrid_prompts()`(call-time) 를 쓴다.
PAGE_HYBRID_SYSTEM_PROMPT = build_system_prompt()
PAGE_HYBRID_USER_PROMPT = build_user_prompt() + _page_hybrid_extra()

# 2026-08-11: 여기 있던 `def build_page_hybrid_prompts()` 재정의를 삭제했다. 위 별칭을 덮으면서
# 본문이 존재하지 않는 `_PAGE_HYBRID_EXTRA` 를 참조해 **호출 즉시 NameError** 였다(현재 함수는
# `_page_hybrid_extra()`). 호출자가 0이라 무증상이었지만 Phase 2 배선이 이 이름을 쓰면 터진다.


__all__ = [
    # 하위 호환성
    'SYSTEM_PROMPT',
    'USER_PROMPT',
    'generate_retry_prompt',
    'get_default_prompts',
    # 모듈화 API (신규)
    'build_system_prompt',
    'build_user_prompt',
    'build_retry_system_prompt',
    'get_relevant_modules_for_violations',
    # 축약 모듈
    'PROMPT_CIRCLED_CHARS_COMPACT',
    # Excel 전용
    'get_excel_prompts',
    'EXCEL_CHART_SYSTEM_PROMPT',
    'EXCEL_CHART_USER_PROMPT',
    'EXCEL_IMAGE_SYSTEM_PROMPT',
    'EXCEL_IMAGE_USER_PROMPT',
    # 다이어그램 전용
    'DIAGRAM_SYSTEM_PROMPT',
    'DIAGRAM_USER_PROMPT',
    # 전면 VL(전사 + 시각 서술) — 이미지 파일·스캔 그림 페이지
    'PAGE_HYBRID_SYSTEM_PROMPT',
    'PAGE_HYBRID_USER_PROMPT',
]
