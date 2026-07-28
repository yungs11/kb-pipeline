"""DelegationRulePlugin — 위임전결표 도메인 플러그인 (SoT §14.4, §22.2).

기본 MatrixTableParser 출력을 그대로 통과시키되:
- matrix_fact 에 "전결권자" 의미를 부여하고
- 같은 행의 fact 들을 묶어 row 단위 delegation_rule chunk 를 추가 생성한다.

합의/수신 같은 메타데이터 컬럼 값의 약어는 ctx.code_map 으로 확장 병기한다.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from ..chunking.chunk_schema import RagChunk
from ..markerutil import is_ambiguous_marker_cell, is_marker_cell
from ..parsers.base import ParseContext
from ..parsers.hierarchy_table import is_full_width_banner, item_numbering_level
from ..textutil import (
    compact,
    is_note_text,
    one_line,
    range_a1,
)
from .base import ParserPlugin

if TYPE_CHECKING:
    from ..canvas.cell_node import CellNode
    from ..canvas.sheet_canvas import SheetCanvas
    from ..detection.region import Region

# 전결표 계열 키워드 (SoT §14.4 contains_keywords)
DELEGATION_KEYWORDS = ("전결", "위임전결", "합의", "수신", "전결권자")

_CURRENCY_UNITS = ("천만원", "백만원", "천원", "만원", "억원", "원")
_CURRENCY_ALT = "|".join(_CURRENCY_UNITS)
_CONTEXT_RE = re.compile(
    rf"단위\s*[:：]\s*(?P<unit>{_CURRENCY_ALT})"
    rf"(?:\s*[,，]\s*(?:VAT|부가세|부가가치세)\s*"
    rf"(?P<vat>별도|제외|포함))?",
    re.IGNORECASE,
)
_NUMBER = r"[+-]?\s*\d[\d,]*(?:\.\d+)?"
_COMPARISON = r"(?:미만|이하|초과|이상)"
_PARTICLE = r"(?:의|을|를|은|는|이|가)"
# 통화 뒤에는 비교어/제한된 조사/비한글 경계만 허용한다. 이로써
# '3백만원이상'은 잡되 '3원칙', '3백만원이상한 조건'은 배제한다.
_CURRENCY_AMOUNT_RE = re.compile(
    rf"{_NUMBER}\s*(?:{_CURRENCY_ALT})(?="
    rf"\s*{_COMPARISON}(?:\s*{_PARTICLE})?(?:\s|[^가-힣]|$)"
    rf"|\s*{_PARTICLE}(?:\s|[^가-힣]|$)"
    rf"|[^가-힣]|$)"
)
_THRESHOLD_RE = re.compile(rf"{_NUMBER}\s*{_COMPARISON}")
_RANGE_RE = re.compile(
    rf"{_NUMBER}\s*(?:~|〜|–|—|부터)\s*{_NUMBER}(?:\s*(?:까지|{_COMPARISON}))?"
)
_NON_MONEY_RE = re.compile(
    rf"(?:%|퍼센트|분의)|"
    rf"{_NUMBER}\s*(?:영업일|개월|시간|일|년|분|초|회|건|명|개|대)\s*(?:{_COMPARISON})?"
)
_PATH_MONEY_CUE_RE = re.compile(
    r"(?:건당|금액|예산|경비|대금|계약|매출|매입|지출|투자|구매|취득|처분|자산|채권)"
)


def _cell_text(cell: "CellNode") -> str:
    """셀의 표시 텍스트 (raw 우선, 병합 logical 보조)."""
    return (
        one_line(cell.normalized_value)
        or one_line(cell.display_value)
        or one_line(cell.raw_value)
        or one_line(cell.logical_value)
    )


def _strip_balanced_outer_wrapper(value: str) -> str:
    """문자열 전체를 감싼 동일 종류 괄호 한 겹만 제거한다."""
    value = value.strip()
    pairs = {"(": ")", "（": "）"}
    closing = pairs.get(value[:1])
    if not closing or not value.endswith(closing):
        return value
    opening = value[0]
    depth = 0
    for index, char in enumerate(value):
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth < 0 or (depth == 0 and index != len(value) - 1):
                return value
    return value[1:-1].strip() if depth == 0 else value


def extract_delegation_context(
    region: "Region", canvas: "SheetCanvas"
) -> Optional[Dict[str, str]]:
    """헤더 앞 표 머리말에서 금액 단위/VAT 문맥을 엄격하게 추출한다.

    같은 의미의 후보는 첫 원본 셀로 dedupe하고, 서로 다른 단위/VAT 후보가
    함께 있으면 어느 쪽도 임의 상속하지 않는다.
    """
    if not region.header_rows:
        return None
    preamble_end = min(region.header_rows) - 1
    if preamble_end < region.min_row:
        return None

    candidates: List[Dict[str, str]] = []
    for row in range(region.min_row, preamble_end + 1):
        for col in range(region.min_col, region.max_col + 1):
            cell = canvas.get_cell(row, col)
            # 병합 복제로 생긴 logical_value는 스캔하지 않고 실제 값 anchor만 본다.
            if cell.raw_value is None:
                continue
            if cell.is_merged and cell.merge_anchor and cell.merge_anchor != cell.address:
                continue
            original = one_line(cell.raw_value)
            if not original:
                continue
            canonical = _strip_balanced_outer_wrapper(original)
            match = _CONTEXT_RE.fullmatch(canonical)
            if not match:
                continue
            context: Dict[str, str] = {
                "금액단위": match.group("unit"),
                "원문": original,
                "source_cell": cell.address,
            }
            vat = match.group("vat")
            if vat:
                context["VAT"] = "별도" if vat in ("별도", "제외") else "포함"
            candidates.append(context)

    if not candidates:
        return None
    semantic = {(c["금액단위"], c.get("VAT", "")) for c in candidates}
    if len(semantic) != 1:
        return None
    return candidates[0]


def _normalized_condition_text(value: Any) -> str:
    text = one_line(value)
    # soft hyphen 및 기타 제어문자는 의미 없는 편집 흔적이므로 제거한다.
    return "".join(char for char in text if not unicodedata.category(char).startswith("C"))


def _has_explicit_currency(value: str) -> bool:
    return bool(_CURRENCY_AMOUNT_RE.search(value))


def _has_bare_threshold(value: str) -> bool:
    if _NON_MONEY_RE.search(value):
        return False
    return bool(_THRESHOLD_RE.search(value) or _RANGE_RE.search(value))


def has_monetary_condition(
    chunk: RagChunk, matrix_role_labels: set[str] | List[str] | Tuple[str, ...]
) -> bool:
    """delegation rule이 금액 조건을 실제로 포함하는지 보수적으로 판별한다."""
    role_labels = {one_line(label) for label in matrix_role_labels if one_line(label)}
    path_text = " > ".join(_normalized_condition_text(p) for p in (chunk.path or []))
    fact_values: List[Tuple[str, str]] = []
    for fact in chunk.facts or []:
        predicate = one_line(fact.get("predicate"))
        value = _normalized_condition_text(fact.get("value"))
        if value:
            fact_values.append((predicate, value))

    # 명시 통화는 역할/비고/path 어디에 있어도 표 전역의 VAT 문맥이 필요하다.
    if _has_explicit_currency(path_text):
        return True
    if any(_has_explicit_currency(value) for _, value in fact_values):
        return True

    # 통화단위 없는 threshold는 역할 fact로 범위를 제한한다.
    if any(
        predicate in role_labels and _has_bare_threshold(value)
        for predicate, value in fact_values
    ):
        return True

    # 경로의 bare threshold는 금액 의미 cue가 함께 있을 때만 인정한다.
    return bool(
        _PATH_MONEY_CUE_RE.search(path_text)
        and _has_bare_threshold(path_text)
    )


def expand_codes(value: str, code_map: Dict[str, str]) -> List[Dict[str, str]]:
    """합의/수신 값의 약어 토큰을 {raw, expanded} 목록으로 변환.

    예: "기,{법(준감)},내" → 기/법/준감/내 각각을 code_map 으로 확장.
    """
    value = one_line(value)
    if not value:
        return []
    tokens: List[str] = []
    for part in re.split(r"[,，/\n]+", value):
        part = part.strip().strip("{}")
        if not part:
            continue
        # 괄호 안 약어는 별도 토큰으로 분리 (중첩 안전)
        inners = re.findall(r"\(([^()]*)\)", part)
        outer = re.sub(r"\([^()]*\)", "", part).strip().strip("()")
        if outer:
            tokens.append(outer)
        for inner in inners:
            inner = inner.strip()
            if inner:
                tokens.append(inner)
    result: List[Dict[str, str]] = []
    seen = set()
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        result.append({"raw": token, "expanded": code_map.get(token, "")})
    return result


def _row_key(chunk: RagChunk) -> int:
    """청크의 원본 엑셀 행(정렬용). source.start_row 우선, 없으면 metadata.excel_row, 그래도 없으면 0."""
    src = chunk.source or {}
    r = src.get("start_row")
    if isinstance(r, int):
        return r
    r = (chunk.metadata or {}).get("excel_row")
    return r if isinstance(r, int) else 0


class DelegationRulePlugin(ParserPlugin):
    name = "delegation_rule"
    priority = 10

    # ------------------------------------------------------------------ match
    def match(self, region: "Region", canvas: "SheetCanvas") -> float:
        if not region.matrix_cols:
            return 0.0
        texts: List[str] = [one_line(region.title)]
        texts.extend(one_line(v) for v in region.matrix_cols.values())
        texts.extend(one_line(v) for v in region.metadata_cols.values())
        for row in region.header_rows:
            for cell in canvas.iter_row(row, region.min_col, region.max_col):
                texts.append(_cell_text(cell))
        haystack = compact(" ".join(t for t in texts if t))
        if any(kw in haystack for kw in DELEGATION_KEYWORDS):
            return 1.0
        return 0.0

    # ------------------------------------------------------------------ parse
    def parse(self, region: "Region", canvas: "SheetCanvas", ctx: ParseContext) -> List[RagChunk]:
        # 타 모듈(parsers.matrix_table)은 지연 import — 범용 파서 위에 얹는 구조 (SoT §14.4)
        from ..parsers.matrix_table import MatrixTableParser

        base_chunks = MatrixTableParser().parse(region, canvas, ctx)

        out: List[RagChunk] = []
        row_paths: Dict[int, List[str]] = {}
        note_rows: set[int] = set()

        for chunk in base_chunks:
            if chunk.chunk_type == "matrix_fact":
                self._annotate_matrix_fact(chunk)
            row = self._single_row_of(chunk)
            if row is not None:
                if chunk.chunk_type == "note":
                    note_rows.add(row)
                path = self._item_path(chunk)
                if path and len(path) >= len(row_paths.get(row, [])):
                    row_paths[row] = path
            out.append(chunk)

        delegation_rows = self._build_delegation_rows(
            region, canvas, ctx, row_paths, note_rows
        )
        inherited_context = extract_delegation_context(region, canvas)
        if inherited_context:
            from ..chunking.sibling_merger import attach_delegation_context

            matrix_role_labels = set(region.matrix_cols.values())
            for rule in delegation_rows:
                if has_monetary_condition(rule, matrix_role_labels):
                    attach_delegation_context(rule, inherited_context)
        out.extend(delegation_rows)
        # delegation_rule 만 형제 병합; 비-delegation 청크는 merge_sibling_rules 가 통과시킴.
        from ..chunking.sibling_merger import merge_sibling_rules

        config = getattr(ctx, "config", None)
        max_chars = getattr(config, "delegation_merge_max_chars", 1100) if config is not None else 1100
        merged = merge_sibling_rules(out, max_chars=max_chars)
        # 엑셀 위→아래 행 순서로 안정정렬. 기존엔 base_chunks(note 등) 뒤에 delegation_rows 를 append 해
        # [note 블록][delegation 블록]으로 뒤섞였음. table_summary=맨앞, section_summary=맨뒤,
        # 그 외 body(note/delegation_rule/matrix_fact/table_row/hierarchy_node/total_row)는 start_row 행순.
        # 병합은 정렬 전 완료 → 형제-run 로직 무영향. 병합 그룹 내부 note 는 그룹 뒤로(블록단위 인터리브).
        _cls = {"table_summary": 0, "section_summary": 2}
        return sorted(merged, key=lambda c: (_cls.get(c.chunk_type, 1), _row_key(c)))

    # ------------------------------------------------------- matrix_fact 보강
    def _annotate_matrix_fact(self, chunk: RagChunk) -> None:
        """matrix_fact 의 열축에 '전결권자' 의미를 부여한다."""
        fields = chunk.fields or {}
        col_axis = (
            one_line(fields.get("열축"))
            or one_line(fields.get("column_axis"))
        )
        if col_axis and "전결권자" not in fields:
            fields["전결권자"] = col_axis
        fields.setdefault("열축의미", "전결권자")
        chunk.fields = fields

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _single_row_of(chunk: RagChunk) -> Optional[int]:
        src = chunk.source or {}
        start, end = src.get("start_row"), src.get("end_row")
        if isinstance(start, int) and start == end:
            return start
        return None

    @staticmethod
    def _item_path(chunk: RagChunk) -> List[str]:
        """chunk.path 에서 표 제목 prefix 를 제거한 항목 경로."""
        path = [one_line(p) for p in (chunk.path or []) if one_line(p)]
        if len(path) > 1 and chunk.title and path[0] == one_line(chunk.title):
            path = path[1:]
        return path

    def _hierarchy_cols(self, region: "Region") -> List[int]:
        if region.hierarchy_cols:
            return list(region.hierarchy_cols)
        if region.matrix_cols:
            first_matrix = min(region.matrix_cols)
            return [c for c in range(region.min_col, first_matrix)]
        return [region.min_col]

    def _body_rows(self, region: "Region") -> List[int]:
        if region.body_rows:
            return sorted(region.body_rows)
        header_max = max(region.header_rows) if region.header_rows else region.min_row - 1
        return [r for r in range(max(region.min_row, header_max + 1), region.max_row + 1)]

    def _row_item_text(self, region: "Region", canvas: "SheetCanvas", row: int) -> Tuple[str, Optional[int], bool]:
        """행의 항목 텍스트. (text, col, came_from_merged_cell)"""
        for col in self._hierarchy_cols(region):
            cell = canvas.get_cell(row, col)
            raw = one_line(cell.normalized_value) or one_line(cell.display_value) or one_line(cell.raw_value)
            if raw:
                return raw, col, False
        for col in self._hierarchy_cols(region):
            cell = canvas.get_cell(row, col)
            logical = one_line(cell.logical_value)
            if logical:
                return logical, col, True
        return "", None, False

    # -------------------------------------------------- delegation_rule 생성
    def _build_delegation_rows(
        self,
        region: "Region",
        canvas: "SheetCanvas",
        ctx: ParseContext,
        row_paths: Dict[int, List[str]],
        note_rows: set,
    ) -> List[RagChunk]:
        chunks: List[RagChunk] = []
        stack: List[str] = []  # MatrixTableParser 경로가 없을 때의 fallback 계층 스택
        title = region.title or ctx.sheet_titles.get(canvas.sheet_name) or ctx.document_title
        matrix_cols = sorted(region.matrix_cols.items())
        metadata_cols = sorted(region.metadata_cols.items())
        hierarchy_cols = self._hierarchy_cols(region)

        for row in self._body_rows(region):
            # --- 경로 추적 (emit 여부와 무관하게 매 행 갱신) ----------------
            known_path = row_paths.get(row)
            item, item_col, came_from_merge = self._row_item_text(region, canvas, row)
            row_is_note = bool(item) and is_note_text(item)

            if known_path:
                stack = list(known_path)
                path = list(known_path)
            elif item and not row_is_note and not came_from_merge:
                level = item_numbering_level(item)
                if level is None:
                    level = hierarchy_cols.index(item_col) if item_col in hierarchy_cols else len(stack)
                level = min(level, len(stack))
                stack = stack[:level] + [item]
                path = list(stack)
            elif stack:
                path = list(stack)
                came_from_merge = True
            elif item and not row_is_note:
                stack = [item]
                path = list(stack)
            else:
                path = []

            if row in note_rows or row_is_note:
                continue  # note 행은 note chunk 가 담당
            if is_full_width_banner(region, canvas, row):
                continue  # 전폭행(섹션/삭제/개정) emit 안 함 — echo 억제(489fb8a 동작 유지)

            # 마커 해석 없이 모든 비계층 열(matrix+metadata)을 header:원문값 으로, 열 순서대로.
            # 빈값·병합 헤더 echo 는 스킵. ○ 는 '○' 원문 유지(정규화 '해당' 아님), 보고/금액/비고 그대로.
            cells: List[Tuple[str, str]] = []
            for col, label in sorted([*matrix_cols, *metadata_cols]):
                value = _cell_text(canvas.get_cell(row, col))
                if not value:
                    continue
                lbl = one_line(label)
                if compact(value) in (compact(lbl), "전결권자"):  # 헤더 echo 제외
                    continue
                cells.append((lbl, value))

            if not cells:
                continue
            if not path:
                path = [f"행 {row}"]

            chunks.append(
                self._make_rule_chunk(
                    region, canvas, ctx, row, path, cells,
                    title=title, came_from_merge=came_from_merge,
                )
            )
        return chunks

    def _make_rule_chunk(
        self,
        region: "Region",
        canvas: "SheetCanvas",
        ctx: ParseContext,
        row: int,
        path: List[str],
        cells: List[Tuple[str, str]],
        *,
        title: Optional[str],
        came_from_merge: bool,
    ) -> RagChunk:
        path_text = " > ".join(path)
        rng = range_a1(row, region.min_col, row, region.max_col)
        kv_str = ", ".join(f"{h}:{v}" for h, v in cells)
        fields: Dict[str, Any] = {
            "항목": path[-1] if path else "",
            "경로": path_text,
            "값": kv_str,
        }
        facts: List[Dict[str, Any]] = [{"predicate": h, "value": v} for h, v in cells]
        base = f"{canvas.sheet_name} 시트 '{path_text}' 항목"  # 병합(_compose_content)과 통일 — document_title 제거
        content = f"{base}: {kv_str}." if kv_str else f"{base}."

        return RagChunk(
            source_file=ctx.source_file,
            sheet=canvas.sheet_name,
            range=rng,
            chunk_type="delegation_rule",
            region_type=region.region_type,
            title=title,
            path=list(path),
            fields=fields,
            facts=facts,
            content_text=content,
            source={
                "file": ctx.source_file,
                "sheet": canvas.sheet_name,
                "range": rng,
                "start_row": row,
                "end_row": row,
                "start_col": region.min_col,
                "end_col": region.max_col,
            },
            metadata={
                "region_id": region.id,
                "sheet_index": canvas.sheet_index,
                "excel_row": row,
                "is_decision_row": True,
                "came_from_merged_cell": came_from_merge,
            },
        )
