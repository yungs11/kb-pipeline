"""kordoc 십진번호(WBS) 계층 병합 — 부모 + 직속 leaf 자식 (Method B, spec 2026-06-26).

self-gating: 다단계 점-번호(1.1.1) 필드가 행의 min_numbered_ratio 이상일 때만 발화.
kordoc table_row(dict) 위에서 동작하며, 병합 청크의 임베딩 텍스트(core_text/embedding_text)도 갱신한다.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, List, Optional

from ..textutil import PARSER_VERSION
from .hierarchy_pack import pack

_DOTTED = re.compile(r"^\s*\d+(?:\.\d+)+")        # 다단계 점-번호(탐지 신호)
_NUM = re.compile(r"^\s*(\d+(?:\.\d+)*)")          # 선두 번호(레벨/부모 파싱)


def _number_of(c: Dict[str, Any], field: str) -> Optional[str]:
    v = (c.get("fields") or {}).get(field)
    if not isinstance(v, str):
        return None
    m = _NUM.match(v)
    return m.group(1) if m else None


def _parent(num: str) -> str:
    return num.rsplit(".", 1)[0] if "." in num else "ROOT"


def _has_children(num: str, allnums: set) -> bool:
    return any(_parent(m) == num for m in allnums if m != num)


def _detect_number_field(rows: List[Dict[str, Any]], min_ratio: float) -> Optional[str]:
    counts: Dict[str, List[int]] = {}  # field -> [match, nonempty]
    for c in rows:
        for k, v in (c.get("fields") or {}).items():
            if not isinstance(v, str) or not v.strip():
                continue
            mc = counts.setdefault(k, [0, 0])
            mc[1] += 1
            if _DOTTED.match(v):
                mc[0] += 1
    best, best_score = None, -1
    for k, (mc, nc) in counts.items():
        if nc and mc / nc >= min_ratio and mc > best_score:
            best, best_score = k, mc
    return best


def _header(parent: Dict[str, Any], field: str) -> str:
    f = parent.get("fields") or {}
    head = f"[{parent.get('sheet', '')}] {f.get(field, '')}"
    ctx = [f"{k}: {f[k]}" for k in ("단계", "Activity", "담당자") if f.get(k)]
    if ctx:
        head += " (" + " / ".join(ctx) + ")"
    return head


def _line(child: Dict[str, Any], field: str) -> str:
    return f"- {(child.get('fields') or {}).get(field, '')}"


def _compose(parent: Dict[str, Any], children: List[Dict[str, Any]], field: str) -> str:
    return _header(parent, field) + "\n" + "\n".join(_line(c, field) for c in children)


def _build_wbs(parent, children, field, part_index, part_total) -> Dict[str, Any]:
    src_file = parent.get("source_file", "")
    sheet = parent.get("sheet", "")
    p_src = parent.get("source") or {}
    last_src = (children[-1].get("source") or {})
    r0 = p_src.get("start_row")
    r1 = last_src.get("end_row") or last_src.get("start_row") or r0
    prng, crng = parent.get("range") or "", children[-1].get("range") or ""
    if prng and crng and ":" in prng and ":" in crng:
        rng = prng.split(":")[0] + ":" + crng.split(":")[1]
    else:
        rng = prng or crng
    num = _number_of(parent, field)
    child_nos = [_number_of(c, field) for c in children]
    content = _compose(parent, children, field)

    facts: List[Dict[str, Any]] = list(parent.get("facts") or [])
    for c in children:
        facts.extend(c.get("facts") or [])
    kws, seen = [], set()
    for c in [parent] + children:
        for k in (c.get("keywords") or []):
            if k not in seen:
                seen.add(k)
                kws.append(k)
    confs = [(c.get("quality") or {}).get("confidence") for c in [parent] + children]
    confs = [v for v in confs if isinstance(v, (int, float))]
    fields = dict(parent.get("fields") or {})
    fields["하위"] = child_nos

    carry = {k: v for k, v in (parent.get("metadata") or {}).items()
             if k in ("workbook_title", "section", "region_id", "sheet_index")}
    return {
        "id": f"{src_file}::{sheet}::{rng}::wbsgrp{part_index}",
        "source_file": src_file, "sheet": sheet, "range": rng,
        "chunk_type": "table_row", "region_type": parent.get("region_type", ""),
        "title": parent.get("title"), "path": list(parent.get("path") or []),
        "fields": fields, "facts": facts, "content_text": content, "keywords": kws[:30],
        "source": {"file": src_file, "sheet": sheet, "range": rng, "start_row": r0,
                   "end_row": r1, "start_col": p_src.get("start_col"), "end_col": p_src.get("end_col")},
        "metadata": {**carry, "merged": True, "merged_count": len(children) + 1,
                     "parent_no": num, "child_nos": child_nos,
                     "part_index": part_index, "part_total": part_total,
                     "core_text": content, "embedding_text": content},
        "quality": {"confidence": round(min(confs), 4) if confs else 0.0,
                    "review_required": any((c.get("quality") or {}).get("review_required") for c in [parent] + children),
                    "parser_version": PARSER_VERSION},
    }


def merge_wbs_rows(chunks: List[Dict[str, Any]], *, max_chars: int = 1100,
                   min_numbered_ratio: float = 0.6) -> List[Dict[str, Any]]:
    if max_chars <= 0:
        return list(chunks)

    rows_by_sheet: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for c in chunks:
        if c.get("chunk_type") == "table_row":
            rows_by_sheet[c.get("sheet")].append(c)
    sheet_field: Dict[Any, Optional[str]] = {}
    sheet_nums: Dict[Any, set] = {}
    for sheet, rows in rows_by_sheet.items():
        field = _detect_number_field(rows, min_numbered_ratio)
        sheet_field[sheet] = field
        if field:
            sheet_nums[sheet] = {n for c in rows if (n := _number_of(c, field))}
    if not any(sheet_field.values()):
        return list(chunks)  # self-gating

    consumed = set()
    out: List[Dict[str, Any]] = []
    n = len(chunks)
    for i, c in enumerate(chunks):
        if id(c) in consumed:
            continue
        sheet = c.get("sheet")
        field = sheet_field.get(sheet)
        if c.get("chunk_type") != "table_row" or not field:
            out.append(c)
            continue
        num = _number_of(c, field)
        if not num:
            out.append(c)
            continue
        allnums = sheet_nums[sheet]
        leaf_children = {m for m in allnums if _parent(m) == num and not _has_children(m, allnums)}
        if not leaf_children:
            out.append(c)  # leaf 노드 또는 내부노드 → 단독
            continue
        children: List[Dict[str, Any]] = []
        j = i + 1
        while j < n:
            d = chunks[j]
            if d.get("sheet") != sheet or d.get("chunk_type") != "table_row":
                break
            if id(d) in consumed:
                break
            dn = _number_of(d, field)
            if dn in leaf_children:
                children.append(d)
                j += 1
                continue
            break
        if not children:
            out.append(c)
            continue
        subgroups = pack(children, measure=lambda g: len(_compose(c, g, field)), max_chars=max_chars)
        total = len(subgroups)
        for part, grp in enumerate(subgroups, start=1):
            out.append(_build_wbs(c, grp, field, part, total))
            for x in grp:
                consumed.add(id(x))
    return out
