"""delegation_rule 연속 형제 병합 (spec 2026-06-26).

같은 region_id + 직속 부모(path[:-1]) 를 가진 연속 행 청크를, content_text 길이가
max_chars 를 넘지 않는 선에서 하나로 묶는다. 단일 행 그룹은 원본을 그대로 반환한다.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from ..textutil import PARSER_VERSION, one_line, range_a1, stable_id
from .chunk_schema import RagChunk
from .hierarchy_pack import assign_parts, pack


def _region_id(c: RagChunk) -> Any:
    return (c.metadata or {}).get("region_id")


def _excel_row(c: RagChunk) -> int:
    md = c.metadata or {}
    src = c.source or {}
    v = md.get("excel_row")
    if isinstance(v, int):
        return v
    v = src.get("start_row")
    return v if isinstance(v, int) else 0


def _parent_key(c: RagChunk) -> Tuple[Any, Tuple[str, ...]]:
    return (_region_id(c), tuple(c.path[:-1]))


def _join(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(one_line(v) for v in value if one_line(v))
    return one_line(value)


def _leaf(c: RagChunk) -> str:
    if c.path:
        return one_line(c.path[-1])
    return one_line((c.fields or {}).get("항목", "")) or f"행 {_excel_row(c)}"


def _line(c: RagChunk) -> str:
    leaf = _leaf(c)
    appr = _join((c.fields or {}).get("전결권자"))
    base = f"- {leaf}: 전결권자 {appr}" if appr else f"- {leaf}"
    extra = []
    for k in ("합의", "수신", "비고", "관계"):
        v = _join((c.fields or {}).get(k))
        if v:
            extra.append(f"{k}: {v}")
    if extra:
        base += " (" + ", ".join(extra) + ")"
    return base


def _compose_content(group: List[RagChunk]) -> str:
    first = group[0]
    sheet = one_line(first.sheet)
    parent = " > ".join(one_line(p) for p in first.path[:-1] if one_line(p))
    subject = parent or one_line(first.title) or sheet
    header = f"{sheet} 시트 '{subject}' 항목군의 전결 기준:"
    return header + "\n" + "\n".join(_line(c) for c in group)


def _build_merged(group: List[RagChunk], part_index: int, part_total: int) -> RagChunk:
    first = group[0]
    parent_path = list(first.path[:-1])
    rows = [_excel_row(c) for c in group]
    cols = [(c.source or {}) for c in group]
    start_col = min((s.get("start_col") for s in cols if isinstance(s.get("start_col"), int)), default=None)
    end_col = max((s.get("end_col") for s in cols if isinstance(s.get("end_col"), int)), default=None)
    r0, r1 = min(rows), max(rows)
    rng = range_a1(r0, start_col, r1, end_col) if (start_col and end_col) else (first.range or "")

    facts: List[Dict[str, Any]] = []
    keywords: List[str] = []
    seen_kw = set()
    for c in group:
        leaf = _leaf(c)
        for f in (c.facts or []):
            nf = dict(f)
            nf.setdefault("subject", leaf)
            facts.append(nf)
        for kw in (c.keywords or []):
            if kw not in seen_kw:
                seen_kw.add(kw)
                keywords.append(kw)

    # NOTE: pipeline finalize_chunk() 가 emit 직전 quality 를 score_quality(chunk, region, ctx)
    # 로 **항상 재계산**한다(chunk_factory.py:138). 아래 min-rollup 은 merge_sibling_rules 를
    # 직접 호출하는 단위 테스트용 기본값일 뿐, 실제 파이프라인에선 덮어쓰인다. 따라서 정확한
    # 재채점을 위해 아래 metadata 에 자식 감점 플래그를 any() 로 전파한다(confidence.py:109-125).
    confs = [(c.quality or {}).get("confidence") for c in group]
    confs = [x for x in confs if isinstance(x, (int, float))]
    quality = {
        "confidence": round(min(confs), 4) if confs else 0.0,
        "review_required": any((c.quality or {}).get("review_required") for c in group),
        "parser_version": PARSER_VERSION,
    }

    def _any_flag(flag: str) -> bool:
        return any((c.metadata or {}).get(flag) for c in group)

    meta: Dict[str, Any] = {
        "region_id": _region_id(first),
        "merged": True,
        "merged_count": len(group),
        "child_rows": rows,
        "child_ranges": [c.range for c in group],
        "part_index": part_index,
        "part_total": part_total,
        "is_decision_row": True,
        # 정보용 플래그 — score_quality 가 읽지 않음(단일행 청크와의 parity 보존용)
        "has_approver": _any_flag("has_approver"),
        "has_special_values": _any_flag("has_special_values"),
        # score_quality 가 항상 읽는 감점 신호(confidence.py:109,112) → bool 로 항상 세팅
        "ambiguous_marker": _any_flag("ambiguous_marker"),
        "came_from_merged_cell": _any_flag("came_from_merged_cell"),
    }
    # score_quality 가 '존재 시'에만 감점하는 신호(confidence.py:114-125) → 참일 때만 키 세팅
    for flag in ("contains_hidden_rows", "contains_hidden_cols", "contains_hidden",
                 "path_uncertain", "missing_row_label", "missing_column_axis"):
        if _any_flag(flag):
            meta[flag] = True

    new_id = stable_id(first.source_file, first.sheet, "delegation_rule", rng,
                       *parent_path, str(part_index))
    return RagChunk(
        id=new_id,
        source_file=first.source_file,
        sheet=first.sheet,
        range=rng,
        chunk_type="delegation_rule",
        region_type=first.region_type,
        title=first.title,
        path=parent_path,
        fields={"경로": " > ".join(parent_path), "항목들": [_leaf(c) for c in group]},
        facts=facts,
        content_text=_compose_content(group),
        keywords=keywords[:30],
        source={
            "file": first.source_file, "sheet": first.sheet, "range": rng,
            "start_row": r0, "end_row": r1, "start_col": start_col, "end_col": end_col,
        },
        metadata=meta,
        quality=quality,
    )


def merge_sibling_rules(chunks: List[RagChunk], *, max_chars: int = 1100) -> List[RagChunk]:
    """입력을 **원래 순서대로** 처리한다. delegation_rule 이 아닌 청크(note, code_mapping,
    matrix_fact 등)는 위치를 보존하며 그대로 통과시킨다. 연속한 delegation_rule 중
    region_id + path[:-1] 가 같은 run 을 모아 캡 단위로 병합한다(전역 정렬 없음 → 재배치 없음).
    입력 delegation_rule 은 문서(행) 순서로 들어온다고 가정한다(플러그인이 body_row 순 생성).
    """
    if max_chars <= 0:
        return list(chunks)

    out: List[RagChunk] = []
    run: List[RagChunk] = []  # 연속 delegation_rule, 모두 같은 parent_key

    def flush() -> None:
        if not run:
            return
        subgroups = pack(list(run), measure=lambda g: len(_compose_content(g)), max_chars=max_chars)
        for g, part_index, part_total in assign_parts(subgroups, multi_only=True):
            if len(g) == 1:
                out.append(g[0])  # 단일 행 → 원본 그대로 (불변)
            else:
                out.append(_build_merged(g, part_index, part_total))
        run.clear()

    for c in chunks:
        if c.chunk_type != "delegation_rule":
            flush()
            out.append(c)  # 비-delegation 통과 (위치 보존)
            continue
        if run and _parent_key(run[-1]) == _parent_key(c):
            run.append(c)
        else:
            flush()
            run.append(c)
    flush()
    return out
