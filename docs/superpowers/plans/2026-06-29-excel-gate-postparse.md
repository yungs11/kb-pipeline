<!-- plan-version: v2 -->
<!-- codex-validation: READY v2 at 2026-06-29T08:50:39Z (codex backend down — verified via ultracode adversarial workflows wf_0f78ad5b-bb4 → NEEDS_REVISION(4) → fixed → wf_5743ac11-356 → READY) -->

# 엑셀 게이트웨이 검증 재설계 (파서 후단 + 추출오류/나란히2표 차단) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** doc_guard 게이트를 파서 후단으로 옮기고, 기존 13규칙을 끈 뒤, 엑셀 추출 실패(#REF!·헤더누수·빈헤더)와 나란히 놓인 무관한 두 표만 차단한다.

**Architecture:** excel-parser 가 파싱 직후 시트별 `gate_summary`(findings + 셀 좌표)를 계산해 `/parse` 응답 `stats` 에 실어 보낸다. doc_guard 신규 `POST /v1/check-excel` 는 그 요약을 받아 판정·한국어 메시지만 만든다(CheckReport 스키마 재사용). knowledge_base 는 게이트 단계(파서 후단)에서 기존 ExcelParserClient 로 `/parse` 를 호출해 요약을 얻고 doc_guard 에 넘긴다. 프론트는 단계 순서를 `파싱→게이트검증→청킹→적재`로 바꾸고 문서규칙 영역을 숨긴다.

**Tech Stack:** Python(FastAPI, openpyxl, pytest), TypeScript/Next.js(React), 3 repos: `7.excel-parser`, `99.projects/shinhan_trust/doc_guard`, `99.projects/shinhan_trust/knowledge_base`.

## Global Constraints
- 차단 finding code 는 정확히 4종: `ref_error`, `header_leak`, `empty_header`, `side_by_side`. 전부 severity=error(경고 없음).
- 참조오류 정규식(verbatim): `#(REF|VALUE|DIV/0|N/A|NAME\?|NULL|NUM)!?`
- 나란히2표 신호: 검출 헤더행에 **동일 컬럼 라벨 중복** 등장(예 법령리스트 `순번` @ A2,C2).
- 셀 좌표는 사람이 읽는 A1 표기(`openpyxl.utils.get_column_letter`).
- 비-xlsx/xlsm 문서는 게이트 무검증 통과. 기존 doc_guard `POST /v1/check`(13규칙)은 호출하지 않는다(코드는 보존).
- doc_guard 응답은 기존 `CheckReport` dict 스키마 재사용(`result/summary/skipped_rules/findings/customer_message`) → 프론트 `GatePopup` 불변.
- 검증 파일(절대경로):
  - 차단: `7.excel-parser/test_doc_excel/신한자산신탁_외부테이터_필요사이트 정리.xlsx`(시트2 법령리스트), `excel-parser-markitdown/test_doc_excel/251210_중소형그룹사_AX추진지원_WBS_v0.1_sys.xlsx`
  - 통과: `/Users/xxx/Downloads/aws_cost_estimate.xlsx`, `excel-parser-markitdown/test_doc_excel/신한자산신탁_자산목록_v20251013.xlsx`(접근제어 적용 대상), `7.excel-parser/test_doc_excel/2-1. 위임전결기준표(2026.04.17. 개정).xlsx`

---

## File Structure
- `7.excel-parser/excel_parser_rag/gate/__init__.py` — 신규 패키지
- `7.excel-parser/excel_parser_rag/gate/excel_gate.py` — 신규: `compute_gate_summary(input_path, chunks) -> dict`
- `7.excel-parser/service/main.py` — 수정: `/parse` 핸들러에서 gate_summary 계산 → `stats["gate_summary"]`
- `7.excel-parser/tests/test_excel_gate.py` — 신규 테스트
- `doc_guard/app/excel_gate_policy.py` — 신규: 요약→CheckReport 변환 + 메시지
- `doc_guard/app/main.py` — 수정: `POST /v1/check-excel`
- `doc_guard/tests/test_check_excel.py` — 신규 테스트
- `knowledge_base/backend/app/clients/excel_parser_client.py` — 수정: 응답에서 `gate_summary` 노출
- `knowledge_base/backend/app/clients/docguard_client.py` — 수정: `check_excel(...)`
- `knowledge_base/backend/app/core/pipeline.py` — 수정: 게이트 후단화(xlsx)
- `knowledge_base/frontend/components/JobList.tsx` — 수정: 단계 재정렬
- `knowledge_base/frontend/components/UploadPanel.tsx` — 수정: 문서규칙 영역 숨김

---

## Task 1: excel-parser — gate 검출 모듈 `compute_gate_summary`

**Files:**
- Create: `7.excel-parser/excel_parser_rag/gate/__init__.py`
- Create: `7.excel-parser/excel_parser_rag/gate/excel_gate.py`
- Test: `7.excel-parser/tests/test_excel_gate.py`

**Interfaces:**
- Produces: `compute_gate_summary(input_path: str | pathlib.Path, chunks: list[dict]) -> dict`
  반환: `{"ok": bool, "sheets": [{"sheet": str, "ok": bool, "findings": [{"code": str, "cells": list[str], "detail": str}]}]}`
  - `code` ∈ {`ref_error`,`header_leak`,`empty_header`,`side_by_side`}; 최상위 `ok` = 모든 시트 finding 0건.
- Consumes: `excel_parser_rag.pipeline.build_canvases`, `detect_and_classify`(헤더행/region), `openpyxl`.

- [ ] **Step 1: Write the failing test**

```python
# 7.excel-parser/tests/test_excel_gate.py
import pathlib
import pytest
from excel_parser_rag.gate.excel_gate import compute_gate_summary
from excel_parser_rag.pipeline import parse_excel_for_rag

ROOT = pathlib.Path("/Users/xxx/workspace")
EXCEL = ROOT / "7.excel-parser/test_doc_excel"
MARK = ROOT / "excel-parser-markitdown/test_doc_excel"

def _summ(path):
    chunks, _ = parse_excel_for_rag(str(path))
    return compute_gate_summary(path, chunks)

def _codes(summary, sheet_substr):
    for s in summary["sheets"]:
        if sheet_substr in s["sheet"]:
            return {f["code"] for f in s["findings"]}
    return set()

def test_side_by_side_blocks_beoplyeong():
    s = _summ(EXCEL / "신한자산신탁_외부테이터_필요사이트 정리.xlsx")
    assert s["ok"] is False
    assert "side_by_side" in _codes(s, "법령리스트")
    # 중복 라벨 셀 좌표가 보고된다
    cells = [c for f in next(x for x in s["sheets"] if "법령리스트" in x["sheet"])["findings"]
             if f["code"] == "side_by_side" for c in f["cells"]]
    assert "A2" in cells and "C2" in cells

def test_ref_error_blocks_wbs():
    s = _summ(MARK / "251210_중소형그룹사_AX추진지원_WBS_v0.1_sys.xlsx")
    assert s["ok"] is False
    wbs_codes = set().union(*[ {f["code"] for f in sh["findings"]} for sh in s["sheets"] ])
    assert "ref_error" in wbs_codes

def test_aws_passes():
    s = _summ(pathlib.Path("/Users/xxx/Downloads/aws_cost_estimate.xlsx"))
    assert s["ok"] is True

def test_external_sheet1_passes():
    s = _summ(EXCEL / "신한자산신탁_외부테이터_필요사이트 정리.xlsx")
    assert _codes(s, "외부데이터소스 현황") == set()

def test_jasan_access_passes():
    s = _summ(MARK / "신한자산신탁_자산목록_v20251013.xlsx")
    assert _codes(s, "접근제어 적용 대상") == set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/xxx/workspace/7.excel-parser && ./.venv/bin/python -m pytest tests/test_excel_gate.py -q`
Expected: FAIL — `ModuleNotFoundError: excel_parser_rag.gate`

- [ ] **Step 3: Write minimal implementation**

```python
# 7.excel-parser/excel_parser_rag/gate/__init__.py
from .excel_gate import compute_gate_summary  # noqa: F401
```

```python
# 7.excel-parser/excel_parser_rag/gate/excel_gate.py
"""게이트 검증 요약 — 추출 실패(ref/header_leak/empty_header) + 나란히2표.

설계: docs/superpowers/specs/2026-06-29-excel-gate-postparse-design.md
백엔드(openpyxl/kordoc) 무관하게 동작: 원시 셀(openpyxl)로 구조/참조,
실제 파싱 chunks 로 헤더누수를 판정한다.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List

import openpyxl
from openpyxl.utils import get_column_letter

from ..config import ParserConfig
from ..pipeline import build_canvases, detect_and_classify

ERROR_RE = re.compile(r"#(REF|VALUE|DIV/0|N/A|NAME\?|NULL|NUM)!?")


def _header_labels(region, canvas) -> Dict[int, str]:
    """region 헤더행의 {col: label}. header_rows 없으면 빈 dict."""
    out: Dict[int, str] = {}
    for hr in (region.header_rows or []):
        for col in range(region.min_col, region.max_col + 1):
            cell = canvas.cells.get((hr, col))
            # CellNode 필드명: display_value / normalized_value / logical_value (cell_node.py).
            # 병합·복원 셀까지 잡으려면 logical_value 우선.
            val = "" if cell is None else ("" if cell.is_empty else str(getattr(cell, "logical_value", "") or cell.display_value or cell.normalized_value or "").strip())
            if val and col not in out:
                out[col] = val
    return out


def compute_gate_summary(input_path, chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
    path = Path(input_path)
    cfg = ParserConfig()
    canvases = build_canvases(path, cfg)
    region_pairs = detect_and_classify(canvases, cfg)

    # region 을 시트별로 묶기
    by_sheet: Dict[str, list] = defaultdict(list)
    for region, canvas in region_pairs:
        by_sheet[canvas.sheet_name].append((region, canvas))

    # 원시 워크북(참조오류 스캔용)
    wb = openpyxl.load_workbook(path, data_only=True)

    sheets_out: List[Dict[str, Any]] = []
    for ws in wb.worksheets:
        findings: List[Dict[str, Any]] = []

        # 1) ref_error — 모든 셀 스캔
        ref_cells: List[str] = []
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and ERROR_RE.search(cell.value):
                    ref_cells.append(f"{get_column_letter(cell.column)}{cell.row}")
        if ref_cells:
            findings.append({"code": "ref_error", "cells": ref_cells[:20],
                             "detail": f"참조 오류가 값에 포함됨: {', '.join(ref_cells[:5])}"})

        # 2)~3) side_by_side / empty_header — region 헤더 기반
        for region, canvas in by_sheet.get(ws.title, []):
            labels = _header_labels(region, canvas)
            if not labels:
                continue
            counts = Counter(labels.values())
            dups = [lab for lab, n in counts.items() if n > 1]
            if dups:
                dup_cells = [f"{get_column_letter(col)}{region.header_rows[0]}"
                             for col, lab in labels.items() if lab in dups]
                findings.append({"code": "side_by_side", "cells": sorted(dup_cells),
                                 "detail": f"헤더 라벨 중복({', '.join(dups)}) — 나란히 놓인 두 표로 판단"})
            # empty_header: 사용 열에 헤더 라벨이 비어있는 칸
            empty_cols = [get_column_letter(col) + str(region.header_rows[0])
                          for col in range(region.min_col, region.max_col + 1) if col not in labels]
            if empty_cols:
                findings.append({"code": "empty_header", "cells": empty_cols[:20],
                                 "detail": f"헤더 컬럼명이 비어있음: {', '.join(empty_cols[:5])}"})

        # 4) header_leak — chunk 의 field[k]==k (헤더행이 데이터로 추출됨)
        for c in chunks:
            if c.get("sheet") != ws.title:
                continue
            fields = c.get("fields") or {}
            if not isinstance(fields, dict) or len(fields) < 2:
                continue
            same = sum(1 for k, v in fields.items()
                       if isinstance(v, str) and v.strip() == str(k).strip() and v.strip() != "")
            if same >= max(2, (len(fields) + 1) // 2):
                src = c.get("source") or {}
                row = src.get("start_row")
                loc = [f"row{row}"] if row else []
                findings.append({"code": "header_leak", "cells": loc,
                                 "detail": "헤더행이 데이터로 추출됨(헤더=값)"})
                break  # 시트당 1건이면 충분

        sheets_out.append({"sheet": ws.title, "ok": not findings, "findings": findings})

    wb.close()
    return {"ok": all(s["ok"] for s in sheets_out), "sheets": sheets_out}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/xxx/workspace/7.excel-parser && ./.venv/bin/python -m pytest tests/test_excel_gate.py -q`
Expected: PASS (5 passed). 만약 `header_leak`/`empty_header` 오탐으로 통과 케이스가 실패하면 임계치를 조정한다(통과 케이스 우선 — aws/외부시트1/접근제어/위임전결은 반드시 ok).

- [ ] **Step 5: Add 위임전결 통과 회귀 테스트 + commit**

```python
# tests/test_excel_gate.py 에 추가
def test_wijum_passes_for_now():
    s = _summ(EXCEL / "2-1. 위임전결기준표(2026.04.17. 개정).xlsx")
    # 향후 고도화 전까지 통과(매트릭스 미차단)
    assert _codes(s, "위임전결") == set()
```

Run: `./.venv/bin/python -m pytest tests/test_excel_gate.py -q` → PASS

```bash
cd /Users/xxx/workspace/7.excel-parser
git add excel_parser_rag/gate tests/test_excel_gate.py
git commit -m "feat(gate): excel gate_summary — ref/header_leak/empty_header/side_by_side detection"
```

---

## Task 2: excel-parser — `/parse` 응답에 `gate_summary` 노출

**Files:**
- Modify: `7.excel-parser/service/main.py` — 공유 파싱 함수 `_run_parse` 안, 파싱 성공(`chunks, stats = get_backend(...).parse(...)`, ~line 119)과 `tmp_path` unlink(~line 122) **사이**에 gate 계산을 삽입한다. **반드시 `_run_parse` 안**에서 한다 — sync(`/parse`, parse_sync:158-184)와 async 잡(`/parse/jobs/file`→work:187-211) **두 경로가 모두 `_run_parse` 를 거치므로 한 번의 편집으로 둘 다 커버**된다. (핸들러에서 따로 하면 `chunks/stats/tmp_path` 가 스코프 밖이고 tmp_path 는 :122에서 이미 unlink됨.)
- Test: `7.excel-parser/tests/test_service_gate_summary.py`

**Interfaces:**
- Consumes: `compute_gate_summary` (Task 1)
- Produces: `/parse` 응답 `stats["gate_summary"]` (Task 1 스키마)

- [ ] **Step 1: Write the failing test**

```python
# 7.excel-parser/tests/test_service_gate_summary.py
import pathlib
from fastapi.testclient import TestClient
from service.main import app

EXCEL = pathlib.Path("/Users/xxx/workspace/7.excel-parser/test_doc_excel")

def test_parse_includes_gate_summary():
    f = EXCEL / "신한자산신탁_외부테이터_필요사이트 정리.xlsx"
    with TestClient(app) as client, f.open("rb") as fh:
        r = client.post("/parse", files={"file": (f.name, fh,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 200
    gs = r.json()["stats"]["gate_summary"]
    assert gs["ok"] is False
    assert any("side_by_side" in {x["code"] for x in s["findings"]} for s in gs["sheets"])

def test_gate_computation_failure_blocks(monkeypatch):
    # gate '계산' 예외는 보수적 차단(ok=False) 이어야 한다 (spec §8).
    import service.main as m
    def boom(*a, **k):
        raise RuntimeError("gate boom")
    monkeypatch.setattr(m, "compute_gate_summary", boom)
    f = EXCEL / "신한자산신탁_외부테이터_필요사이트 정리.xlsx"
    with TestClient(app) as client, f.open("rb") as fh:
        r = client.post("/parse", files={"file": (f.name, fh,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 200
    assert r.json()["stats"]["gate_summary"]["ok"] is False
```

> 주의: `service.main` 이 `from excel_parser_rag.gate import compute_gate_summary` 를 **모듈 최상단에서 import** 해 `m.compute_gate_summary` 로 참조해야 monkeypatch 가 먹는다.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/xxx/workspace/7.excel-parser && ./.venv/bin/python -m pytest tests/test_service_gate_summary.py -q`
Expected: FAIL — `KeyError: 'gate_summary'`

- [ ] **Step 3: Write minimal implementation**

`service/main.py` 의 공유 함수 `_run_parse` 안, 파싱 성공 직후 / `tmp_path` unlink 직전에 삽입(in-scope `chunks/stats/tmp_path` 사용 → sync·async 두 경로 동시 커버):

```python
# service/main.py _run_parse — chunks, stats = get_backend(...).parse(tmp_path, config) 직후, tmp unlink 전
from excel_parser_rag.gate import compute_gate_summary
try:
    stats["gate_summary"] = compute_gate_summary(tmp_path, chunks)
except Exception as exc:
    # 게이트 '계산' 실패는 보수적으로 차단(ok=False) — spec §8 "추출 불가 = 적재 불가".
    # (genuine /parse 실패는 :119 에서 이미 500 으로 표면화되어 여기 도달 안 함.)
    stats["gate_summary"] = {"ok": False, "sheets": [], "error": str(exc)}
```

> 보수적 차단 근거: gate 계산 자체가 터지면 헤더:값 추출 신뢰성을 보장 못 하므로 통과시키지 않는다. doc_guard 는 이 경우 `findings` 가 없어도 `ok==False` 면 차단 메시지("검증 처리 중 오류 — 잠시 후 재시도/문의")를 낸다(Task 3 참조). 만약 운영상 soft-pass 가 필요해지면 §11 결정으로 명시 후 변경.

> ⚠️ 구현 주의 2건(재검증에서 지적):
> 1. `from excel_parser_rag.gate import compute_gate_summary` 를 **`service/main.py` 모듈 최상단**에 둔다(`_run_parse` 안 지역 import 금지) — 그래야 Step 1 의 `monkeypatch.setattr(m, "compute_gate_summary", ...)` 가 except 경로를 친다.
> 2. gate 계산은 **`tmp_path` unlink(`finally`, ~:122) 전, `try` 블록 안**에 둔다 — unlink 뒤에 두면 tmp_path 가 사라져 `compute_gate_summary` 가 매번 실패→전건 과차단된다.

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/test_service_gate_summary.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/xxx/workspace/7.excel-parser
git add service/main.py tests/test_service_gate_summary.py
git commit -m "feat(service): expose gate_summary in /parse stats"
```

---

## Task 3: doc_guard — `POST /v1/check-excel`

**Files:**
- Create: `doc_guard/app/excel_gate_policy.py`
- Modify: `doc_guard/app/main.py`
- Test: `doc_guard/tests/test_check_excel.py`

**Interfaces:**
- Consumes: gate_summary(Task 1 스키마)
- Produces: `POST /v1/check-excel` body `{"filename": str, "gate_summary": dict}` →
  CheckReport dict `{result, summary:{error,warning}, skipped_rules:[], findings:[{rule_id,rule_name,severity,page,page_is_approx,location,snippet,matched_text,truncated,message}], customer_message}`
- `build_excel_report(filename: str, gate_summary: dict) -> dict`

- [ ] **Step 1: Write the failing test**

```python
# doc_guard/tests/test_check_excel.py
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

FAIL_SUMMARY = {"ok": False, "sheets": [
    {"sheet": "법령리스트", "ok": False, "findings": [
        {"code": "side_by_side", "cells": ["A2", "C2"], "detail": "헤더 '순번' 중복"}]},
    {"sheet": "현황", "ok": True, "findings": []},
]}
PASS_SUMMARY = {"ok": True, "sheets": [{"sheet": "S", "ok": True, "findings": []}]}

def test_check_excel_fail():
    r = client.post("/v1/check-excel", json={"filename": "x.xlsx", "gate_summary": FAIL_SUMMARY})
    assert r.status_code == 200
    body = r.json()
    assert body["result"] == "fail"
    assert body["summary"]["error"] >= 1
    assert any(f["rule_id"] == "side_by_side" for f in body["findings"])
    assert "A2" in body["customer_message"] and "엑셀" in body["customer_message"]

def test_check_excel_pass():
    r = client.post("/v1/check-excel", json={"filename": "x.xlsx", "gate_summary": PASS_SUMMARY})
    assert r.json()["result"] == "pass"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/xxx/workspace/99.projects/shinhan_trust/doc_guard && python -m pytest tests/test_check_excel.py -q`
Expected: FAIL — 404 (엔드포인트 없음)

- [ ] **Step 3: Write minimal implementation**

```python
# doc_guard/app/excel_gate_policy.py
from __future__ import annotations
from typing import Any, Dict, List

RULE_NAMES = {
    "ref_error": "참조 오류(#REF! 등)",
    "header_leak": "헤더 누수(헤더가 값으로 추출)",
    "empty_header": "빈 헤더/컬럼명",
    "side_by_side": "나란히 놓인 두 표",
    "gate_error": "검증 처리 오류",
}
FIX_HINT = {
    "ref_error": "수식 오류를 정리한 뒤 다시 업로드해주세요.",
    "header_leak": "1행(또는 데이터 위 첫 행)을 헤더로, 그 아래를 값으로 정리해주세요.",
    "empty_header": "비어있는 헤더 칸에 컬럼명을 채워주세요.",
    "side_by_side": "시트를 분리해 한 시트에 표 하나만 두세요.",
    "gate_error": "검증 처리 중 오류가 발생했습니다. 잠시 후 다시 시도하거나 관리자에게 문의해주세요.",
}


def build_excel_report(filename: str, gate_summary: Dict[str, Any]) -> Dict[str, Any]:
    findings: List[Dict[str, Any]] = []
    # gate '계산' 오류(ok=False 인데 sheets/ findings 가 비어있는 경우) → 보수적 차단 (spec §8).
    if gate_summary.get("ok") is False and not any(
        s.get("findings") for s in gate_summary.get("sheets", [])
    ):
        findings.append({
            "rule_id": "gate_error", "rule_name": RULE_NAMES["gate_error"], "severity": "error",
            "page": 0, "page_is_approx": False, "location": filename,
            "snippet": str(gate_summary.get("error", "")), "matched_text": "", "truncated": False,
            "message": f"검증 처리 중 오류로 적재할 수 없습니다. {FIX_HINT['gate_error']}",
        })
    for sheet in gate_summary.get("sheets", []):
        for f in sheet.get("findings", []):
            code = f.get("code", "")
            cells = ", ".join(f.get("cells", []))
            loc = f"{sheet.get('sheet')}!{cells}" if cells else str(sheet.get("sheet"))
            findings.append({
                "rule_id": code,
                "rule_name": RULE_NAMES.get(code, code),
                "severity": "error",
                "page": 0,
                "page_is_approx": False,
                "location": loc,
                "snippet": f.get("detail", ""),
                "matched_text": cells,
                "truncated": False,
                "message": f"[{sheet.get('sheet')}] {f.get('detail','')} — {FIX_HINT.get(code,'')}",
            })
    result = "fail" if findings else "pass"
    if findings:
        lines = [f"- {x['message']}" for x in findings]
        customer = ("엑셀에서 다음 문제가 발견되어 적재할 수 없습니다. "
                    "해당 위치를 수정 후 다시 업로드해주세요.\n" + "\n".join(lines))
    else:
        customer = "검출된 위반 항목이 없습니다."
    return {
        "result": result,
        "summary": {"error": len(findings), "warning": 0},
        "skipped_rules": [],
        "findings": findings,
        "customer_message": customer,
    }
```

```python
# doc_guard/app/main.py — 추가
from pydantic import BaseModel
from app.excel_gate_policy import build_excel_report

class ExcelCheckRequest(BaseModel):
    filename: str
    gate_summary: dict

@app.post("/v1/check-excel")
def check_excel(req: ExcelCheckRequest) -> dict:
    return build_excel_report(req.filename, req.gate_summary)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_check_excel.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/xxx/workspace/99.projects/shinhan_trust/doc_guard
git add app/excel_gate_policy.py app/main.py tests/test_check_excel.py
git commit -m "feat(doc_guard): /v1/check-excel — gate_summary to CheckReport"
```

---

## Task 4: knowledge_base — ExcelParserClient gate_summary 노출 + docguard_client.check_excel

**Files:**
- Modify: `knowledge_base/backend/app/clients/excel_parser_client.py`
- Modify: `knowledge_base/backend/app/clients/docguard_client.py`
- Test: `knowledge_base/backend/tests/test_docguard_check_excel.py`

**Interfaces:**
- Produces: `ExcelParseResult.gate_summary: dict | None` (from_response 에서 `body["stats"]["gate_summary"]` 추출)
- Produces: `DocGuardClient.check_excel(self, gate_summary: dict, filename: str) -> dict[str, Any]` — `POST {base}/v1/check-excel`

- [ ] **Step 1: Write the failing test**

```python
# knowledge_base/backend/tests/test_docguard_check_excel.py
import httpx
from app.clients.docguard_client import DocGuardClient

def test_check_excel_posts_summary():
    captured = {}
    def handler(request):
        captured["url"] = str(request.url)
        captured["json"] = httpx.Response(200).json if False else None
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"result": "fail", "summary": {"error": 1, "warning": 0},
                                         "findings": [], "customer_message": "x"})
    client = DocGuardClient("http://dg", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    out = client.check_excel({"ok": False, "sheets": []}, "a.xlsx")
    assert captured["url"].endswith("/v1/check-excel")
    assert captured["body"]["filename"] == "a.xlsx"
    assert out["result"] == "fail"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base/backend && python -m pytest tests/test_docguard_check_excel.py -q`
Expected: FAIL — `AttributeError: check_excel`

- [ ] **Step 3: Write minimal implementation**

```python
# knowledge_base/backend/app/clients/docguard_client.py — 메서드 추가
def check_excel(self, gate_summary: dict, filename: str) -> dict:
    """파서 후단 엑셀 게이트 — gate_summary 를 doc_guard 로 보내 CheckReport 받음."""
    url = f"{self._base_url}/v1/check-excel"
    resp = self._client.post(url, json={"filename": filename, "gate_summary": gate_summary})
    resp.raise_for_status()
    return resp.json()
```

```python
# knowledge_base/backend/app/clients/excel_parser_client.py — ExcelParseResult 에 필드 추가
# @dataclass 에 추가:
#   gate_summary: dict | None = None
# from_response 안에서:
#   gate_summary=(body.get("stats") or {}).get("gate_summary"),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_docguard_check_excel.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base
git add backend/app/clients/docguard_client.py backend/app/clients/excel_parser_client.py backend/tests/test_docguard_check_excel.py
git commit -m "feat(kb): docguard.check_excel + ExcelParseResult.gate_summary"
```

---

## Task 5: knowledge_base — 게이트 후단화(xlsx) in pipeline

**Files:**
- Modify: `knowledge_base/backend/app/core/pipeline.py:452-497` (Stage0 게이트)
- Test: `knowledge_base/backend/tests/test_pipeline_excel_gate.py`

**Interfaces:**
- Consumes: `deps.excel_parser.parse(...)`(기존), `ExcelParseResult.gate_summary`(Task 4), `deps.docguard.check_excel`(Task 4)
- Produces: xlsx 일 때 `status="rejected"` + `gate_popup` (기존 `_build_gate_popup`/`set_doc_guard_result` 재사용)

- [ ] **Step 1: Write the failing test** (게이트 분기만 단위 검증 — fake deps)

```python
# knowledge_base/backend/tests/test_pipeline_excel_gate.py
# xlsx + gate fail → rejected; xlsx + gate pass → 진행; 비-xlsx → 게이트 통과(13규칙 미호출)
# 기존 pipeline 테스트 패턴(FakeDeps)을 따른다. 핵심 단언:
#  - filename 이 .xlsx 이고 excel_parser.parse().gate_summary.ok==False →
#       IngestResult.status == "rejected" 이고 docguard.check 가 호출되지 않음(check_excel 만)
#  - filename 이 .pdf → docguard.check / check_excel 둘 다 호출 안 됨(무검증 통과)
```

> 구현자는 `backend/tests/` 의 기존 pipeline 테스트(FakeDeps/FakeRepo) 패턴을 그대로 재사용해 위 3개 케이스를 작성한다. 새 fake 메서드: `excel_parser.parse(...)` 가 `gate_summary` 를 가진 결과를, `docguard.check_excel(...)` 가 CheckReport 를 반환하도록 스텁.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pipeline_excel_gate.py -q`
Expected: FAIL (현재는 원시바이트 13규칙 게이트라 분기 없음)

- [ ] **Step 3: Write minimal implementation**

먼저 `pipeline.py:452-497` 의 현재 코드를 정독한다(실제 변수명·분기 보존이 핵심). 현재 동작(검증된 사실): `report` 는 :470-472 에서 무조건 `deps.docguard.check(...)` 로 할당됨; rejected 분기는 기존 문서면 `prepare_existing_document`, 신규면 `create_document` 로 `rec` 생성(:476-489) 후 `IngestResult(status="rejected", document_id=rec.document_id, gate_popup=..., detail=...)` 반환(:493); 그리고 게이트 블록 **밖** :532 에 `deps.repo.set_doc_guard_result(rec.document_id, report)` 공유 라인이 있음.

기존 원시바이트 13규칙 게이트(`deps.docguard.check(...)`, :470)를 아래로 교체하되 **스코프를 깨지 않게** 한다:

```python
# Stage: 게이트(파서 후단). 엑셀(xlsx/xlsm)만 검증, 그 외는 무검증 통과.
report = None  # ← 게이트 블록 진입 전 반드시 초기화(비-xlsx 경로에서 :532 NameError 방지)
ext = (filename.rsplit(".", 1)[-1].lower() if "." in filename else "")
if ext in ("xlsx", "xlsm"):
    parse_res = deps.excel_parser.parse(file_bytes=file_bytes, file_name=filename)  # ★ 키워드 전용, file_name (not filename)
    gate_summary = getattr(parse_res, "gate_summary", None) or {"ok": True, "sheets": []}
    report = deps.docguard.check_excel(gate_summary, filename)
    if report.get("result") == "fail":
        # 기존 rejected 경로의 rec 생성 분기(prepare_existing_document vs create_document, :476-489)를
        # 그대로 재사용해 rec 을 만든다.
        rec = (deps.repo.prepare_existing_document(...) if <기존 문서 조건>
               else deps.repo.create_document(...))
        deps.repo.set_doc_guard_result(rec.document_id, report)
        return IngestResult(
            status="rejected",
            document_id=rec.document_id,          # ★ 기존 :493 과 동일하게 유지
            gate_popup=_build_gate_popup(report),
            detail="게이트 차단(엑셀 형식). 표시된 위치를 수정 후 재업로드하세요.",
        )
    # ★ pass 분기에서 set_doc_guard_result 를 호출하지 않는다(여기엔 rec 이 없음).
    #    기록은 게이트 블록 밖 :532 의 공유 라인이 담당한다.
# 비-xlsx → report 는 None 그대로(무검증 통과). deps.docguard.check(13규칙) 호출은 삭제.
```

그리고 게이트 블록 **밖** :532 의 공유 라인을 가드한다:

```python
# (기존) deps.repo.set_doc_guard_result(rec.document_id, report)
# (변경) report 가 있을 때만 기록 — 비-xlsx/통과 시 report 는 None 일 수 있음
if report is not None:
    deps.repo.set_doc_guard_result(rec.document_id, report)
```

> 구현자 주의:
> - `deps.excel_parser.parse` 는 **키워드 전용** 시그니처 `(self, *, file_bytes, file_name, options=None)` 이다(client 코드 확인). 위치인자 금지.
> - `report = None` 초기화와 :532 가드를 **반드시** 넣는다(없으면 비-xlsx 에서 NameError).
> - pass 분기에 `set_doc_guard_result` 를 넣지 않는다(`rec` 미생성 → NameError + :532 중복).
> - rejected 반환에 `document_id=rec.document_id` 유지, `<기존 문서 조건>` 의 prepare/create 분기는 원본(:476-489) 그대로 옮긴다.
> - `deps.docguard.check`(13규칙) 호출은 **삭제**.
> UI 단계 emit: 게이트가 파서 후단이므로 `파싱` stage 다음에 `gate` 가 표기되도록 게이트 stage 이벤트를 excel_parser.parse 호출 직후 emit(기존 stage emit 헬퍼). tail 내부 재파싱은 stage 중복 emit 하지 않는다.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_pipeline_excel_gate.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base
git add backend/app/core/pipeline.py backend/tests/test_pipeline_excel_gate.py
git commit -m "feat(kb): relocate gate to post-parse, excel-only via check_excel"
```

---

## Task 6: knowledge_base 프론트 — 단계 재정렬 + 문서규칙 숨김

**Files:**
- Modify: `knowledge_base/frontend/components/JobList.tsx` (`KB_PIPELINE_STAGE_ORDER`)
- Modify: `knowledge_base/frontend/components/UploadPanel.tsx` (문서 가드 규칙 영역)

- [ ] **Step 1: 단계 순서 재정렬**

`JobList.tsx` 의 `KB_PIPELINE_STAGE_ORDER` 를 다음으로 변경(파싱→게이트검증→청킹→적재):

```ts
const KB_PIPELINE_STAGE_ORDER: { key: string; label: string }[] = [
  { key: "parse", label: "파싱" },
  { key: "gate", label: "게이트검증" },
  { key: "chunk", label: "청킹" },
  { key: "insert", label: "적재" },
];
```

`DEFAULT_STAGE_ORDER`(비-kb_pipeline)도 `gate`→`dify` 순이면 그대로 두되, kb_pipeline 어휘 분기(`KB_PIPELINE_STAGES`)는 변경 없음.

- [ ] **Step 2: 문서규칙 영역 숨김**

`UploadPanel.tsx` 에서 "문서 가드 규칙" 블록(헤더 "문서 가드 규칙" ~ 체크박스 목록, 약 167–193행 + 관련 state/`listDocguardRules` 호출/`disabledRules` 전송)을 렌더에서 제거한다. 업로드 호출 시 `disabledRules` 인자는 보내지 않는다(전송 생략).

- [ ] **Step 3: 빌드/타입 체크**

Run: `cd /Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base/frontend && pnpm tsc --noEmit`
Expected: 타입 에러 0 (제거한 `rules`/`enabled` state 미사용 변수 없도록 정리)

- [ ] **Step 4: Commit**

```bash
cd /Users/xxx/workspace/99.projects/shinhan_trust/knowledge_base
git add frontend/components/JobList.tsx frontend/components/UploadPanel.tsx
git commit -m "feat(kb-ui): reorder stages parse→gate, hide doc-guard rules panel"
```

---

## Task 7: 통합 스모크 (실파일)

**Files:**
- Test: `7.excel-parser/tests/test_gate_acceptance.py` (excel-parser 레벨 수용 기준 — 서비스 미기동으로 가능)

- [ ] **Step 1: 수용 기준 테스트 작성**

```python
# 7.excel-parser/tests/test_gate_acceptance.py
import pathlib
from excel_parser_rag.gate.excel_gate import compute_gate_summary
from excel_parser_rag.pipeline import parse_excel_for_rag

ROOT = pathlib.Path("/Users/xxx/workspace")
CASES_BLOCK = [
    (ROOT/"7.excel-parser/test_doc_excel/신한자산신탁_외부테이터_필요사이트 정리.xlsx", "법령리스트"),
    (ROOT/"excel-parser-markitdown/test_doc_excel/251210_중소형그룹사_AX추진지원_WBS_v0.1_sys.xlsx", None),
]
CASES_PASS = [
    pathlib.Path("/Users/xxx/Downloads/aws_cost_estimate.xlsx"),
    ROOT/"excel-parser-markitdown/test_doc_excel/신한자산신탁_자산목록_v20251013.xlsx",
    ROOT/"7.excel-parser/test_doc_excel/2-1. 위임전결기준표(2026.04.17. 개정).xlsx",
]

def _summ(p):
    ch, _ = parse_excel_for_rag(str(p)); return compute_gate_summary(p, ch)

def test_block_cases():
    for path, sheet in CASES_BLOCK:
        s = _summ(path)
        assert s["ok"] is False, f"{path.name} should be blocked"

def test_pass_cases():
    for path in CASES_PASS:
        s = _summ(path)
        assert s["ok"] is True, f"{path.name} should pass: {s}"
```

- [ ] **Step 2: 실행**

Run: `cd /Users/xxx/workspace/7.excel-parser && ./.venv/bin/python -m pytest tests/test_gate_acceptance.py -q`
Expected: PASS. 실패 시 Task 1 임계치/규칙을 조정(통과 케이스 우선 보장).

- [ ] **Step 3: 서비스 기동 E2E (수동, 선택)**

excel-parser(:18055)·doc_guard(:8000) 기동 후:
```bash
# excel-parser → gate_summary 확인
curl -s -F "file=@/Users/xxx/workspace/7.excel-parser/test_doc_excel/신한자산신탁_외부테이터_필요사이트 정리.xlsx" \
  http://localhost:18055/parse | python -c "import sys,json;print(json.load(sys.stdin)['stats']['gate_summary']['ok'])"
# → False 기대
```

- [ ] **Step 4: Commit**

```bash
cd /Users/xxx/workspace/7.excel-parser
git add tests/test_gate_acceptance.py
git commit -m "test(gate): acceptance — block/pass real files"
```

---

## Self-Review (작성자 체크 결과)
- **Spec coverage:** §4 excel-parser→Task1·2 / §5 doc_guard→Task3 / §6 knowledge_base 백+프론트→Task4·5·6 / §9 테스트→Task1·7. 전부 매핑됨.
- **Type 일관성:** gate_summary 스키마(ok/sheets/findings{code,cells,detail})가 Task1↔3↔5 동일. `check_excel(gate_summary, filename)` 시그니처 Task4↔5 일치.

## v2 검증 반영 (ultracode adversarial workflow wf_0f78ad5b-bb4 — codex 백엔드 장애 대체)
검증자+회의론자 13에이전트, ~466k 토큰. **NEEDS_REVISION(4 must-fix)** → 전부 반영:
1. **[a] parse-failure 정책**: gate 계산을 `_run_parse` 안(파싱성공~tmp unlink 사이)으로 이동(sync+async 동시 커버). except → `ok:False`(보수적 차단). doc_guard `build_excel_report` 가 findings 없이 `ok:False` 여도 `gate_error` 로 차단. 회귀 테스트 추가. (Task2·Task3)
2. **[c] CellNode 필드명**: `display/normalized` → `display_value/normalized_value`(+`logical_value` 우선). (Task1)
3. **[d] parse 시그니처**: 키워드 전용 `parse(file_bytes=..., file_name=filename)`. (Task5)
4. **[e] pipeline 스코프**: `report=None` 초기화 + :532 `if report is not None` 가드 + pass분기 set_doc_guard_result 제거 + rejected 반환 `document_id=rec.document_id` 및 prepare/create 분기 보존. (Task5)
- **SOUND 확인**: [b] header_leak — PASS 4파일 ~11,358 chunk 실증 오탐 0건. [f] doc_guard 스키마 — CheckReport/Finding + GatePopup 소비 필드 일치.
