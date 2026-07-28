"""HierarchyTableParser — 매트릭스가 아닌 계층표 파서 (SoT §13, §17.3, §17.4).

공용 계층 헬퍼도 이 모듈에 둔다 (MatrixTableParser 가 재사용):
- HierarchyTracker  : 항목 번호 패턴 + 컬럼 위치 fallback 기반 hierarchy stack (SoT §13.4)
- detect_item_text  : hierarchy_cols 에서 항목 텍스트 추출 (raw 우선, 병합 logical 보조, SoT §13.5)
- SectionCollector  : 최상위 path 단위 section_summary 집계 (SoT §17.2)
"""

from __future__ import annotations

import re
from collections import Counter, OrderedDict
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, TYPE_CHECKING

from openpyxl.utils import get_column_letter, range_boundaries

from ..chunking.chunk_schema import RagChunk
from ..textutil import (
    _SPINE_VALUE_RE,
    infer_numbering_level,
    is_note_text,
    is_spine_column,
    is_total_text,
    one_line,
    row_content,
)
from .base import BaseRegionParser, ParseContext
from .flat_table import body_rows_of, cell_text, flatten_headers, merged_text, region_row_text

if TYPE_CHECKING:
    from ..canvas.sheet_canvas import SheetCanvas
    from ..detection.region import Region

# '5-1.', '5-2.' 같은 복합 번호 섹션 라벨 — 'N.' 섹션과 동급의 최상위 (SoT §13.3 보강).
# textutil.infer_numbering_level 은 'N.' 만 level 0 으로 보므로, 'N-M.' 이 직전의
# '가./나.' 깊이에 끌려 들어가 잘못 중첩되는 것을 여기서 막는다.
_COMPOUND_TOP_NUMBER_RE = re.compile(r"^\d+(?:\s*-\s*\d+)+\s*\.")


def item_numbering_level(value: Any) -> Optional[int]:
    """항목 번호 패턴 레벨. 'N-M.' 복합 번호는 'N.' 과 같은 최상위(0)로 본다."""
    t = one_line(value)
    if not t:
        return None
    if _COMPOUND_TOP_NUMBER_RE.match(t):
        return 0
    return infer_numbering_level(t)


# ---------------------------------------------------------------------------
# 계층 헬퍼
# ---------------------------------------------------------------------------

def marker_style(text: str) -> Optional[str]:
    """마커의 '종류'(스타일)를 반환. 레벨이 아니라 종류 — 종류가 바뀌면 계층이 한 단계 깊어진다.

    고정 레벨맵(item_numbering_level)과 달리 임의 마커를 임의 깊이로 처리한다. 무마커(평문
    카테고리)·기호(○)는 None. 입력은 NFKC 정규화됨(cell_text) 가정 — 유니코드 로마자/원문자는
    ascii 로 온다(ⅱ→ii, Ⅰ→I, ①→"1"[점없음→None]). item_numbering_level 인식 마커는 전부 커버
    (parity — 불릿/대문자라틴/paren-latin/제N장 포함)해 조상소실을 막는다.
    """
    t = (text or "").strip()
    if not t:
        return None
    if re.match(r"^제\s*\d+\s*[장조절편관]", t):    # 제1장/제2조/제3절 — parity(level0)
        return "je"
    if re.match(r"^\d+[-.]\d+[.)\s]", t):        # 5-1. / 1.1  (trailing punct/space)
        return "dec"
    if re.match(r"^\(\d+\)", t):                 # (1)
        return "pnum"
    if re.match(r"^\([가-힣]\)", t):              # (가)
        return "pkor"
    if re.match(r"^\([a-zA-Z]\)", t):            # (a)/(A) — parity(level3)
        return "platin"
    if re.match(r"^\d+[.)](?!\d)", t):           # 1. / 1)  — (?!\d)로 '3.5백만원' 값 배제
        return "num"
    if t[0] in "가나다라마바사아자차카타파하" and re.match(r"^[가-힣][.)]", t):  # 가.
        return "kor"
    if re.match(r"^[IVXLCDM]+[.)]", t):          # I./V./X. (로마 대문자)
        return "uroman"
    if re.match(r"^[ivxlcdm]+[.)]", t):          # i)/ii)/iii) (NFKC→ascii)
        return "lroman"
    if re.match(r"^[A-Z][.)]", t):               # A./B. — parity(level1)
        return "ulatin"
    if re.match(r"^[a-z][.)]", t):               # a)
        return "alpha"
    if re.match(r"^[-·•▪]\s", t):                # - · • ▪ — parity(level5)
        return "bullet"
    return None


class SectionStack:
    """전폭 섹션배너 행들의 계층. marker_style 전환으로 nesting(번호섹션 형제, 새 스타일 딥)."""

    def __init__(self):
        self._chain: List[Tuple[Optional[str], str]] = []

    def push(self, text: str) -> None:
        t = (text or "").strip()
        if not t:
            return
        st = marker_style(t)
        ch = self._chain
        if st is not None:
            styles = [s for s, _ in ch]
            if st in styles:
                i = styles.index(st); del ch[i:]; ch.append((st, t))
            else:
                ch.append((st, t))
        else:
            ch.clear(); ch.append((None, t))  # predicate 상 도달 안 함(marker≠None) — 안전 fallback

    @property
    def path(self) -> List[str]:
        return [t for _, t in self._chain]


def is_full_width_banner(region, canvas, row) -> bool:
    """region 폭의 ≥80% 를 가로지르는 단일 행 병합인가(geometric). 섹션/echo 판정은 호출부."""
    width = region.max_col - region.min_col + 1
    if width < 3:
        return False
    for mr in canvas.merged_ranges:
        try:
            c0, r0, c1, r1 = range_boundaries(mr)
        except Exception:
            continue
        if r0 <= row <= r1 and (c1 - c0 + 1) >= 0.8 * width:
            return True
    return False


class HierarchyTracker:
    """항목 번호 패턴(우선) + 컬럼 위치(fallback) 으로 hierarchy stack 을 유지한다.

    stack 항목을 (level, text) 쌍으로 들고 있어 레벨이 비연속(0 → 3)이어도
    sibling 끼리 잘못 중첩되지 않는다.
    """

    def __init__(self, hierarchy_cols: Sequence[int]):
        self.hier_cols = list(hierarchy_cols)
        self._items: List[Tuple[int, str]] = []
        self._last_col: Optional[int] = None
        self._last_level: int = -1
        self._col_levels: Dict[int, int] = {}

    def infer_level(self, text: str, col: int) -> int:
        level = item_numbering_level(text)
        if level is not None:
            return level
        # 번호 패턴이 없으면 컬럼 위치로 추정 (SoT §13.2)
        if self._last_col is not None:
            if col > self._last_col:
                return self._last_level + 1
            if col == self._last_col:
                return max(self._last_level, 0)
            remembered = self._col_levels.get(col)
            if remembered is not None:
                return remembered
        if col in self.hier_cols:
            return self.hier_cols.index(col)
        return (self._last_level + 1) if self._last_col is not None else 0

    def push(self, text: str, col: int) -> List[str]:
        level = max(0, self.infer_level(text, col))
        while self._items and self._items[-1][0] >= level:
            self._items.pop()
        self._items.append((level, text))
        self._last_col = col
        self._last_level = level
        self._col_levels[col] = level
        return self.path

    @property
    def path(self) -> List[str]:
        return [text for _, text in self._items]

    @property
    def top(self) -> str:
        return self._items[0][1] if self._items else ""

    @property
    def last_item(self) -> str:
        return self._items[-1][1] if self._items else ""


class ColHierarchyTracker:
    """계층열마다 독립 번호 sub-stack 을 유지하고 path 를 열순 concat 으로 만든다.

    HierarchyTracker(단일 전역 스택)와 달리, 깊은 열의 항목이 얕은 열 항목을 pop 하지 못한다
    → 세로 병합된 좌측 계층열 셀이 그 span 의 부모로 유지된다(SoT §13.4 보강). 열 내부 깊이는
    marker_style 전환으로 산정: 새 스타일=한 단계 깊어짐, 같은 스타일=형제, 기존 스타일 재등장=
    그 레벨로 복귀. 얕은 열이 갱신되면 그보다 깊은 열 체인은 무효화한다.

    가정: 무마커 카테고리는 마커 항목보다 얕은(왼쪽) 열에 있다(무마커 push 는 그 열 체인을
    리셋한다). 같은 스타일이 한 열에서 더 깊은 레벨로 재등장하면 얕은 레벨로 붕괴(→ doc_guard 안내).
    """

    def __init__(self, hierarchy_cols: Sequence[int], spine_cols: Set[int] = frozenset()):
        self.cols = sorted(hierarchy_cols)
        self.spine_cols = set(spine_cols)
        self.chains: Dict[int, List[Tuple[Optional[str], str]]] = {c: [] for c in self.cols}

    def push(self, col: int, text: str) -> List[str]:
        if col not in self.chains:
            return self.path
        t = (text or "").strip()
        # spine 열(dotted-int WBSID): dot-경계 prefix 체인 의미론.
        if col in self.spine_cols and _SPINE_VALUE_RE.match(t):
            ch = self.chains[col]
            # 부모 = dot-경계 prefix (1.1.1 의 부모 1.1). prefix 아닌 항목 pop 후 append.
            # ('1.1.1'+'.' 은 '1.1.10' 의 prefix 아님 → 2자리 세그 경계 정확).
            while ch and not t.startswith(ch[-1][1] + "."):
                ch.pop()
            ch.append(("spine", t))
            for c in self.cols:
                if c > col:
                    self.chains[c].clear()  # 얕은 열 갱신 → 깊은 열 무효화
            return self.path
        st = marker_style(text)
        ch = self.chains[col]
        if st is not None:
            styles = [s for s, _ in ch]
            if st in styles:            # 기존 스타일 재등장 → 그 레벨로 복귀(형제)
                i = styles.index(st)
                del ch[i:]
                ch.append((st, text))
            else:                       # 새 스타일 → 한 단계 깊어짐
                ch.append((st, text))
        else:                           # 무마커(카테고리) = 그 열 대표값(리셋)
            ch.clear()
            ch.append((None, text))
        for c in self.cols:
            if c > col:
                self.chains[c].clear()  # 얕은 열 갱신 → 깊은 열 무효화
        return self.path

    @property
    def path(self) -> List[str]:
        out: List[str] = []
        for c in self.cols:
            out.extend(text for _, text in self.chains[c])
        return out

    def labeled_path(self, labels: Dict[int, str]) -> List[Tuple[str, str]]:
        """path 와 동일 순서로 (헤더라벨, 값) 튜플 리스트. 라벨 없는 열은 ''."""
        out: List[Tuple[str, str]] = []
        for c in self.cols:
            label = labels.get(c) or ""
            for _s, t in self.chains[c]:
                out.append((label, t))
        return out

    @property
    def top(self) -> str:
        p = self.path
        return p[0] if p else ""

    @property
    def last_item(self) -> str:
        p = self.path
        return p[-1] if p else ""


def detect_item_text(
    canvas: "SheetCanvas", row: int, hier_cols: Sequence[int], tracker: HierarchyTracker
) -> Tuple[str, Optional[int], bool]:
    """행에서 항목 텍스트를 찾는다 → (text, col, came_from_merged_cell).

    raw 값 우선, 없으면 세로/블록 병합 logical 값 보조 (SoT §13.5).
    병합 logical 이 현재 stack 최상단과 같으면 같은 항목의 연속 행으로 보고
    text="" + came_from_merged_cell=True 를 반환한다.
    """
    for c in hier_cols:
        text = cell_text(canvas.get_cell(row, c))
        if text:
            return text, c, False
    for c in hier_cols:
        cell = canvas.get_cell(row, c)
        logical = merged_text(cell, ("vertical", "block"))
        if not logical:
            continue
        if logical == tracker.last_item:
            return "", c, True  # 직전 항목의 병합 연장
        return logical, c, True
    return "", None, False


class SectionCollector:
    """최상위 path 단위 section_summary 집계 (SoT §17.2)."""

    def __init__(self) -> None:
        self._sections: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    def _get(self, name: str, row: int) -> Dict[str, Any]:
        sec = self._sections.get(name)
        if sec is None:
            sec = {"rows": 0, "children": set(), "axes": Counter(), "min_row": row, "max_row": row}
            self._sections[name] = sec
        sec["min_row"] = min(sec["min_row"], row)
        sec["max_row"] = max(sec["max_row"], row)
        return sec

    def touch(self, name: str, row: int) -> None:
        if name:
            self._get(name, row)

    def add_data_row(self, name: str, row: int, axes: Sequence[str] = ()) -> None:
        if not name:
            return
        sec = self._get(name, row)
        sec["rows"] += 1
        for axis in axes:
            sec["axes"][axis] += 1

    def add_child(self, name: str, row: int, child: str) -> None:
        if not name or not child or child == name:
            return
        self._get(name, row)["children"].add(child)

    def build_chunks(
        self,
        parser: BaseRegionParser,
        region: "Region",
        canvas: "SheetCanvas",
        ctx: ParseContext,
        unit_label: str = "데이터 행",
        confidence: float = 0.86,
    ) -> List[RagChunk]:
        chunks: List[RagChunk] = []
        for name, sec in self._sections.items():
            chunk = parser.new_chunk(
                region, canvas, ctx, "section_summary", min_row=sec["min_row"], max_row=sec["max_row"]
            )
            top_axes = [axis for axis, _ in sec["axes"].most_common(3)]
            chunk.path = [name]
            chunk.fields = {
                "섹션": name,
                "데이터행수": sec["rows"],
                "하위항목수": len(sec["children"]),
                "주요열축": top_axes,
            }
            text = (
                f"{ctx.document_title}의 {canvas.sheet_name} 시트에서 '{name}' 섹션은 "
                f"{sec['rows']}개의 {unit_label}과 {len(sec['children'])}개의 하위 항목을 포함한다."
            )
            if top_axes:
                text += f" 주요 열축은 {', '.join(top_axes)}이다."
            chunk.content_text = text
            chunk.quality = {"confidence": confidence}
            chunks.append(chunk)
        return chunks


# ---------------------------------------------------------------------------
# HierarchyTableParser
# ---------------------------------------------------------------------------

class HierarchyTableParser(BaseRegionParser):
    """계층표: 행마다 table_row(path 포함) / hierarchy_node / note + table_summary."""

    name = "hierarchy_table"
    region_types = ("hierarchical_table",)

    row_confidence = 0.88
    node_confidence = 0.82
    note_confidence = 0.82
    total_confidence = 0.85
    summary_confidence = 0.9

    def parse(self, region: "Region", canvas: "SheetCanvas", ctx: ParseContext) -> List[RagChunk]:
        headers = flatten_headers(region, canvas)
        hier_cols = list(region.hierarchy_cols) or [region.min_col]
        # 계층열 라벨 — use_region_cols=False 원시 헤더 스캔(값열 headers dict 과 역할 분리)
        raw_names = flatten_headers(region, canvas, use_region_cols=False)
        hier_labels = {c: raw_names.get(c) or "" for c in hier_cols}
        meta_cols = {int(c): str(n) for c, n in region.metadata_cols.items()}
        value_cols = [
            c for c in range(region.min_col, region.max_col + 1)
            if c not in hier_cols
        ]
        # spine 재판정 — hier_cols 각 열의 body 값이 dotted-int spine(WBSID)이면 prefix-체인 의미론.
        # 검출(_detect_hierarchy_cols)과 동일 헬퍼·동일 body 산정(body_rows_of)으로 detection-parse 일치.
        spine_cols: Set[int] = set()
        for c in hier_cols:
            col_texts = [cell_text(canvas.get_cell(r, c)) for r in body_rows_of(region, canvas)]
            if is_spine_column([t for t in col_texts if t]):
                spine_cols.add(c)
        tracker = ColHierarchyTracker(hier_cols, spine_cols=spine_cols)
        self._spine_cols = spine_cols  # Test 3 detection-parse 일치 단언용(내부 상태 노출)
        sections = SectionCollector()
        body_chunks: List[RagChunk] = []
        stats = {"rows": 0, "nodes": 0, "notes": 0, "totals": 0}

        for r in body_rows_of(region, canvas):
            if not canvas.row_has_content(r, region.min_col, region.max_col):
                continue
            item_text, _item_col, from_merge = detect_item_text(canvas, r, hier_cols, tracker)

            if item_text and is_note_text(item_text):
                body_chunks.append(self._note_chunk(region, canvas, ctx, r, item_text, tracker.path))
                stats["notes"] += 1
                sections.touch(tracker.top, r)
                continue

            values = self._row_values(canvas, r, value_cols, headers, meta_cols)

            if item_text and is_total_text(item_text):
                body_chunks.append(self._total_chunk(region, canvas, ctx, r, item_text, values, tracker.path))
                stats["totals"] += 1
                continue

            # 다중열: 이 행의 모든 계층열 raw 값을 얕은→깊은 순서로 각 열 체인에 push.
            # 병합 continuation(빈 raw)·note·total 은 push 안 함(부모 붕괴 방지).
            # spine_leaf: 이 행이 spine 열에 실제 dotted-int 값을 push 하는가 —
            # ColHierarchyTracker.push 의 spine 가드(`col in spine_cols and _SPINE_VALUE_RE.match`)와
            # 동일 조건 복제(tracker 내부 판정은 외부로 안 나오므로 sibling_rule 신호용 재판정).
            row_spine_leaf = False
            for c in sorted(hier_cols):
                t = cell_text(canvas.get_cell(r, c))
                if t and not is_note_text(t) and not is_total_text(t):
                    tracker.push(c, t)
                    if c in spine_cols and _SPINE_VALUE_RE.match(t):
                        row_spine_leaf = True
            path = tracker.path

            if values:
                body_chunks.append(
                    self._row_chunk(
                        region, canvas, ctx, r, path, values, from_merge,
                        tracker.labeled_path(hier_labels), row_spine_leaf,
                    )
                )
                stats["rows"] += 1
                sections.add_data_row(tracker.top, r, axes=list(values.keys()))
                if len(path) > 1:
                    sections.add_child(tracker.top, r, path[-1])
            elif item_text:
                body_chunks.append(self._hierarchy_chunk(region, canvas, ctx, r, path))
                stats["nodes"] += 1
                sections.touch(tracker.top, r)
                if len(path) > 1:
                    sections.add_child(tracker.top, r, path[-1])

        for r in region.footer_rows:
            text = region_row_text(canvas, region, r)
            if text:
                body_chunks.append(self._note_chunk(region, canvas, ctx, r, text, []))
                stats["notes"] += 1

        chunks: List[RagChunk] = [self._table_summary(region, canvas, ctx, headers, stats)]
        chunks.extend(body_chunks)
        chunks.extend(sections.build_chunks(self, region, canvas, ctx))
        # sibling_rule 파생 — 같은 (region, plain path) 연속 table_row 묶음(원본 유지, append).
        # HierarchyTableParser 한정 훅(MatrixTableParser 금지 — DelegationRulePlugin 내부호출 blast).
        from ..chunking.sibling_rule import build_sibling_rules

        config = getattr(ctx, "config", None)
        max_chars = getattr(config, "sibling_rule_max_chars", 1100) if config is not None else 1100
        chunks.extend(build_sibling_rules(body_chunks, max_chars))
        return chunks

    # --- 내부 ---------------------------------------------------------------

    def _row_values(
        self,
        canvas: "SheetCanvas",
        row: int,
        value_cols: List[int],
        headers: Dict[int, str],
        meta_cols: Dict[int, str],
    ) -> Dict[str, str]:
        values: Dict[str, str] = {}
        for c in value_cols:
            cell = canvas.get_cell(row, c)
            value = cell_text(cell) or merged_text(cell, ("vertical",))
            if not value:
                continue
            key = meta_cols.get(c) or headers.get(c) or get_column_letter(c)
            values.setdefault(key, value)
        return values

    def _row_chunk(
        self,
        region: "Region",
        canvas: "SheetCanvas",
        ctx: ParseContext,
        row: int,
        path: List[str],
        values: Dict[str, str],
        from_merge: bool,
        labeled_path: List[Tuple[str, str]],
        spine_leaf: bool = False,
    ) -> RagChunk:
        chunk = self.new_chunk(region, canvas, ctx, "table_row", min_row=row, max_row=row)
        path_text = " > ".join(path) if path else (chunk.title or "")
        chunk.path = list(path)
        chunk.fields = {"항목": path[-1] if path else "", "경로": path_text, **values}
        chunk.facts = [{"predicate": k, "value": v} for k, v in values.items()]
        # content 만 계층열 헤더라벨 렌더링(chunk.path/fields["경로"] 는 plain 유지)
        chunk.content_text = row_content(
            ctx.document_title, canvas.sheet_name, labeled_path,
            list(values.items()), title=chunk.title or "",
        )
        chunk.metadata["came_from_merged_cell"] = from_merge
        # spine_leaf 행(자기 WBSID 를 path leaf 로 push): sibling_rule 이 부모 그룹핑하도록 신호.
        if spine_leaf:
            chunk.metadata["spine_leaf"] = True
        chunk.quality = {"confidence": self.row_confidence}
        return chunk

    def _hierarchy_chunk(
        self, region: "Region", canvas: "SheetCanvas", ctx: ParseContext, row: int, path: List[str]
    ) -> RagChunk:
        chunk = self.new_chunk(region, canvas, ctx, "hierarchy_node", min_row=row, max_row=row)
        path_text = " > ".join(path)
        chunk.path = list(path)
        chunk.fields = {"항목": path[-1] if path else "", "경로": path_text}
        chunk.content_text = (
            f"{ctx.document_title}의 {canvas.sheet_name} 시트에서 '{path_text}' 항목은 "
            f"하위 항목을 포함하는 상위 항목이다."
        )
        chunk.quality = {"confidence": self.node_confidence}
        return chunk

    def _note_chunk(
        self,
        region: "Region",
        canvas: "SheetCanvas",
        ctx: ParseContext,
        row: int,
        text: str,
        path: List[str],
    ) -> RagChunk:
        chunk = self.new_chunk(region, canvas, ctx, "note", min_row=row, max_row=row)
        related = " > ".join(path) if path else (chunk.title or ctx.document_title)
        chunk.path = list(path) if path else ([chunk.title] if chunk.title else [])
        chunk.fields = {"주석": text}
        chunk.content_text = f"{ctx.document_title}의 {related} 관련 주석: {text}"
        chunk.quality = {"confidence": self.note_confidence}
        return chunk

    def _total_chunk(
        self,
        region: "Region",
        canvas: "SheetCanvas",
        ctx: ParseContext,
        row: int,
        item_text: str,
        values: Dict[str, str],
        path: List[str],
    ) -> RagChunk:
        chunk = self.new_chunk(region, canvas, ctx, "total_row", min_row=row, max_row=row)
        chunk.path = list(path) + [item_text]
        chunk.fields = {"항목": item_text, **values}
        chunk.facts = [{"predicate": k, "value": v} for k, v in values.items()]
        sentences = ", ".join(f"{k}: {v}" for k, v in values.items())
        chunk.content_text = (
            f"{ctx.document_title}의 {canvas.sheet_name} 시트에서 '{item_text}' 행은 합계 행이다"
            + (f": {sentences}." if sentences else ".")
        )
        chunk.metadata["is_total"] = True
        chunk.quality = {"confidence": self.total_confidence}
        return chunk

    def _table_summary(
        self,
        region: "Region",
        canvas: "SheetCanvas",
        ctx: ParseContext,
        headers: Dict[int, str],
        stats: Dict[str, int],
    ) -> RagChunk:
        summary = self.new_chunk(region, canvas, ctx, "table_summary")
        cols = [headers[c] for c in sorted(headers)][:12]
        summary.path = [summary.title] if summary.title else []
        summary.fields = {
            "범위": region.range_a1,
            "데이터행수": stats["rows"],
            "상위항목수": stats["nodes"],
            "주석수": stats["notes"],
            "컬럼": cols,
        }
        col_text = f" 값 컬럼은 {', '.join(cols)}이다." if cols else ""
        summary.content_text = (
            f"{ctx.document_title}의 {canvas.sheet_name} 시트에 있는 '{summary.title}' 표는 "
            f"계층형 표로, 총 {stats['rows']}개의 데이터 행과 {stats['nodes']}개의 상위 항목, "
            f"{stats['notes']}개의 주석을 포함한다.{col_text}"
        )
        summary.quality = {"confidence": self.summary_confidence}
        return summary
