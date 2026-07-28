"""CodeMappingParser — 코드/약어 매핑 표 파서 (SoT §10.2 code_mapping_table, §17.7).

Index 시트처럼 한 region 안에 코드/의미 컬럼 쌍이 여러 개(B/C, E/F) 있을 수 있고,
'▼' 같은 구분자 행과 잔재(고아 셀)가 섞일 수 있어 방어적으로 추출한다.

build_code_map() 은 pipeline 이 파싱 시작 전에 호출해 ctx.code_map 을 만든다.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple, TYPE_CHECKING

from ..chunking.chunk_schema import RagChunk
from ..chunking.hierarchy_pack import pack
from ..textutil import (
    compact,
    infer_numbering_level,
    is_note_text,
    looks_like_code,
    one_line,
    stable_id,
)
from .base import BaseRegionParser, ParseContext
from .flat_table import cell_text

if TYPE_CHECKING:
    from ..canvas.sheet_canvas import SheetCanvas
    from ..detection.region import Region

# ▼ 등 섹션 구분자 행 (Index 시트의 개정 전/후 구분 등)
_SEPARATOR_RE = re.compile(r"^[▼▽▲△■□◆◇●○◎▶◀=\-—_·~〓]+$")

# 'CPO(개인정보보호책임자)' 같은 약어+괄호 설명 코드 — 괄호 안 설명을 떼고 약어만 판정
_CODE_WITH_PAREN_RE = re.compile(r"^([A-Za-z가-힣0-9]{1,8})\s*\(([^()]{1,24})\)$")


def _is_separator(text: str) -> bool:
    t = compact(text)
    return bool(t) and bool(_SEPARATOR_RE.fullmatch(t))


def _code_like(text: str) -> bool:
    """looks_like_code 확장: 'CPO(개인정보보호책임자)' 류 약어(설명) 패턴 허용 (SoT §8.3)."""
    if looks_like_code(text):
        return True
    m = _CODE_WITH_PAREN_RE.fullmatch(one_line(text))
    return bool(m and looks_like_code(m.group(1)))


def _candidate_pairs(region: "Region", canvas: "SheetCanvas") -> List[Tuple[int, int]]:
    """(코드 컬럼, 의미 컬럼) 쌍 후보를 빈도 기반으로 찾는다. 겹치는 컬럼은 강한 쌍 우선."""
    header_rows = set(region.header_rows)
    counts: Dict[Tuple[int, int], int] = {}
    for r in range(region.min_row, region.max_row + 1):
        if r in header_rows:
            continue
        for c in range(region.min_col, region.max_col + 1):
            code = cell_text(canvas.get_cell(r, c))
            if not code or _is_separator(code) or not _code_like(code):
                continue
            for c2 in (c + 1, c + 2):  # 바로 옆 또는 폭 좁은 구분 컬럼 1개 건너뛰기
                if c2 > region.max_col:
                    break
                meaning = cell_text(canvas.get_cell(r, c2))
                if meaning:
                    if not _is_separator(meaning):
                        counts[(c, c2)] = counts.get((c, c2), 0) + 1
                    break
    pairs: List[Tuple[int, int]] = []
    used: set = set()
    for (c, c2), n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if n < 2:
            continue
        if c in used or c2 in used:
            continue
        pairs.append((c, c2))
        used.update((c, c2))
    return sorted(pairs)


def _valid_mapping(code: str, meaning: str) -> bool:
    if not code or not meaning or code == meaning:
        return False
    if _is_separator(code) or _is_separator(meaning):
        return False
    if code.startswith("*") or meaning.startswith("*"):  # 각주
        return False
    if not _code_like(code):
        return False
    if len(meaning) < 2:
        return False
    if infer_numbering_level(meaning) is not None:  # "(가) 3천만원 초과" 같은 잔재
        return False
    if is_note_text(code) or is_note_text(meaning):
        return False
    return True


def extract_mappings(region: "Region", canvas: "SheetCanvas") -> List[Dict[str, Any]]:
    """region 에서 [{code, meaning, row, code_col, meaning_col}] 을 추출한다."""
    out: List[Dict[str, Any]] = []
    header_rows = set(region.header_rows)
    for code_col, meaning_col in _candidate_pairs(region, canvas):
        for r in range(region.min_row, region.max_row + 1):
            if r in header_rows:
                continue
            code = cell_text(canvas.get_cell(r, code_col))
            meaning = cell_text(canvas.get_cell(r, meaning_col))
            if not _valid_mapping(code, meaning):
                continue
            out.append({
                "code": code,
                "meaning": meaning,
                "row": r,
                "code_col": code_col,
                "meaning_col": meaning_col,
            })
    return out


def build_code_map(region_canvas_pairs: List[Tuple["Region", "SheetCanvas"]]) -> Dict[str, str]:
    """code_mapping_table region 들에서 약어 -> 의미 dict 추출 (중복 키는 첫 값 우선)."""
    code_map: Dict[str, str] = {}
    for region, canvas in region_canvas_pairs:
        for m in extract_mappings(region, canvas):
            code_map.setdefault(m["code"], m["meaning"])
    return code_map


class CodeMappingParser(BaseRegionParser):
    name = "code_mapping"
    region_types = ("code_mapping_table",)

    mapping_confidence = 0.78

    def parse(self, region: "Region", canvas: "SheetCanvas", ctx: ParseContext) -> List[RagChunk]:
        # 낱개 쌍 수집: 동일 (약어,의미) 중복만 제거하고, 다른 의미의 같은 약어는 보존
        # (개정 전/후 대조 블록의 상충 매핑을 게이트가 읽을 수 있도록 리스트로 유지).
        pairs: List[Dict[str, Any]] = []
        seen: set = set()
        for m in extract_mappings(region, canvas):
            key = (m["code"], m["meaning"])
            if key in seen:
                continue
            seen.add(key)
            pairs.append(m)
        if not pairs:
            return []

        title = region.title or "약어 매핑"
        config = getattr(ctx, "config", None)
        max_chars = getattr(config, "code_mapping_merge_max_chars", 1100) if config is not None else 1100

        if max_chars <= 0:
            # 롤백 스위치: 기존 낱개 청크 동작 유지
            return [self._single_chunk(region, canvas, ctx, title, m) for m in pairs]

        # 리전당 병합: content 길이가 cap 을 넘으면 part 로 분할
        subgroups = pack(
            pairs,
            measure=lambda g: len(self._compose(ctx, canvas, region, title, g)),
            max_chars=max_chars,
        )
        total = len(subgroups)
        return [
            self._merged_chunk(region, canvas, ctx, title, g, idx, total)
            for idx, g in enumerate(subgroups, start=1)
        ]

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _compose(ctx: ParseContext, canvas: "SheetCanvas", region: "Region",
                 title: str, group: List[Dict[str, Any]]) -> str:
        """병합 content_text: '{doc} > {sheet} > {title?} 약어 매핑:\n- 약어: 의미' 형태."""
        header_bits = [one_line(ctx.document_title), one_line(canvas.sheet_name), one_line(region.title)]
        header = " > ".join(b for b in header_bits if b) + " 약어 매핑:"
        lines = [f"- {m['code']}: {m['meaning']}" for m in group]
        return header + "\n" + "\n".join(lines)

    def _single_chunk(self, region: "Region", canvas: "SheetCanvas", ctx: ParseContext,
                      title: str, m: Dict[str, Any]) -> RagChunk:
        chunk = self.new_chunk(
            region, canvas, ctx, "code_mapping",
            min_row=m["row"], max_row=m["row"],
            min_col=m["code_col"], max_col=m["meaning_col"],
        )
        chunk.title = title
        chunk.path = [title, m["code"]]
        chunk.fields = {"약어": m["code"], "의미": m["meaning"]}
        chunk.facts = [{"predicate": "약어의미", "value": m["meaning"]}]
        chunk.content_text = (
            f"{ctx.document_title}에서 코드 또는 약어 '{m['code']}'의 의미는 '{m['meaning']}'이다."
        )
        chunk.quality = {"confidence": self.mapping_confidence}
        return chunk

    def _merged_chunk(self, region: "Region", canvas: "SheetCanvas", ctx: ParseContext,
                      title: str, group: List[Dict[str, Any]],
                      part_index: int, part_total: int) -> RagChunk:
        r0 = min(m["row"] for m in group)
        r1 = max(m["row"] for m in group)
        c0 = min(m["code_col"] for m in group)
        c1 = max(m["meaning_col"] for m in group)
        chunk = self.new_chunk(
            region, canvas, ctx, "code_mapping",
            min_row=r0, max_row=r1, min_col=c0, max_col=c1,
        )
        chunk.title = title
        chunk.path = [title]
        mapping = [{"약어": m["code"], "의미": m["meaning"]} for m in group]
        # 매핑은 리스트 — 중복 약어(다른 의미)도 별도 항목으로 보존(dict-collapse 금지).
        chunk.fields = {"약어수": len(mapping), "매핑": mapping}
        chunk.facts = [
            {"predicate": "약어의미", "subject": m["code"], "value": m["meaning"]} for m in group
        ]
        # content_text 를 반드시 직접 설정(비면 chunk_factory 가 드롭).
        chunk.content_text = self._compose(ctx, canvas, region, title, group)
        chunk.quality = {"confidence": self.mapping_confidence}
        chunk.metadata.update({"merged": True, "merged_count": len(mapping)})
        if part_total > 1:
            chunk.metadata.update({"part_index": part_index, "part_total": part_total})
            # 분할 시 id 에 part_index 를 포함해 청크 고유성 보장(sibling_rule 규칙).
            chunk.id = stable_id(
                ctx.source_file, canvas.sheet_name, "code_mapping",
                chunk.range, *chunk.path, str(part_index),
            )
        return chunk
