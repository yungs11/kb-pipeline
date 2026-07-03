"""content_text 생성기 (SoT §19).

chunk_type 별 한국어 템플릿으로 임베딩용 자연어 문장을 만든다.
fields 가 많으면 핵심 필드 우선 최대 8개까지만 문장화한다 (SoT §33.2).
"""

from __future__ import annotations

from typing import Any, Dict, List, TYPE_CHECKING

from ..textutil import marker_label_ko, one_line

if TYPE_CHECKING:
    from ..chunking.chunk_schema import RagChunk
    from ..detection.region import Region
    from ..parsers.base import ParseContext

# SoT §33.2 — 문장화에 포함할 핵심 필드 우선순위
PRIORITY_FIELD_KEYS = (
    "항목",
    "경로",
    "전결권자",
    "합의",
    "수신",
    "행축",
    "열축",
    "값",
    "정규화값",
    "약어",
    "의미",
    "코드",
    "구분",
    "비고",
)
MAX_FIELDS_IN_TEXT = 8

# 문장화에서 제외할 내부/중복 필드
_SKIP_FIELD_KEYS = {"경로"}  # path 는 본문 앞부분에 이미 들어간다


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(one_line(v) for v in value if one_line(v))
    if isinstance(value, dict):
        return ", ".join(f"{one_line(k)}={one_line(v)}" for k, v in value.items() if one_line(v))
    return one_line(value)


def _ordered_fields(fields: Dict[str, Any], limit: int = MAX_FIELDS_IN_TEXT) -> List[tuple]:
    """핵심 필드 우선 최대 limit 개 (key, str_value) 반환."""
    out: List[tuple] = []
    seen = set()
    for key in PRIORITY_FIELD_KEYS:
        if key in fields and key not in _SKIP_FIELD_KEYS:
            text = _stringify(fields[key])
            if text:
                out.append((key, text))
                seen.add(key)
        if len(out) >= limit:
            return out
    for key, value in fields.items():
        if key in seen or key in _SKIP_FIELD_KEYS:
            continue
        text = _stringify(value)
        if not text:
            continue
        out.append((key, text))
        if len(out) >= limit:
            break
    return out


def _field_sentences(fields: Dict[str, Any]) -> str:
    return ", ".join(f"{k}는 {v}" for k, v in _ordered_fields(fields))


def _path_text(chunk: "RagChunk") -> str:
    return " > ".join(p for p in chunk.path if one_line(p))


def _first_of(fields: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        if key in fields:
            text = _stringify(fields[key])
            if text:
                return text
    return ""


def build_content_text(chunk: "RagChunk", region: "Region", ctx: "ParseContext") -> str:
    """SoT §19.2 템플릿 기반 content_text 생성. 만들 수 없으면 빈 문자열."""
    doc = one_line(ctx.document_title) or one_line(ctx.source_file)
    sheet = one_line(chunk.sheet)
    title = one_line(chunk.title) or one_line(region.title) or doc
    path_text = _path_text(chunk)
    fields = chunk.fields or {}
    ct = chunk.chunk_type

    if ct == "table_summary":
        base = f"{doc}의 {sheet} 시트에는 '{title}' 표가 있다."
        sentences = _field_sentences(fields)
        if sentences:
            base += f" 주요 정보: {sentences}."
        return base

    if ct == "section_summary":
        subject = path_text or title
        base = f"{doc}의 {sheet} 시트에서 '{subject}' 섹션의 요약이다."
        sentences = _field_sentences(fields)
        if sentences:
            base += f" {sentences}."
        return base

    if ct == "table_row":
        sentences = _field_sentences(fields)
        subject = path_text or title
        if sentences:
            return (
                f"{doc}의 {sheet} 시트에서 '{subject}' 항목은 다음 값을 가진다: "
                f"{sentences}. 원본 위치는 {chunk.range}이다."
            )
        return f"{doc}의 {sheet} 시트에서 '{subject}' 항목 행이다. 원본 위치는 {chunk.range}이다."

    if ct == "hierarchy_node":
        subject = path_text or title
        return f"{doc}의 {sheet} 시트에서 '{subject}' 항목은 하위 항목을 포함하는 상위 항목이다."

    if ct == "matrix_fact":
        row_axis = _first_of(fields, "행축", "row_axis") or path_text or title
        col_axis = _first_of(fields, "열축", "column_axis", "전결권자")
        normalized = _first_of(fields, "정규화값", "normalized_value")
        raw_value = _first_of(fields, "값", "value", "raw_value")
        value_text = marker_label_ko(normalized) if normalized else raw_value
        if not col_axis or not value_text:
            sentences = _field_sentences(fields)
            if not sentences:
                return ""
            return f"{doc}의 {sheet} 시트에서 '{row_axis}' 항목: {sentences}."
        return f"{doc}의 {sheet} 시트에서 '{row_axis}' 항목은 '{col_axis}'에 대해 '{value_text}'이다."

    if ct == "form_field":
        name = _first_of(fields, "field_name", "필드명", "항목")
        value = _first_of(fields, "field_value", "값", "내용")
        if not name and fields:
            # fields 가 {key: value} 단일 쌍인 경우
            for k, v in fields.items():
                name, value = one_line(k), _stringify(v)
                break
        if not name:
            return ""
        return f"{doc}에서 '{name}' 항목의 값은 '{value}'이다."

    if ct == "form_summary":
        base = f"{doc}의 {sheet} 시트에 있는 양식 문서 요약이다."
        sentences = _field_sentences(fields)
        if sentences:
            base += f" {sentences}."
        return base

    if ct == "code_mapping":
        code = _first_of(fields, "약어", "코드", "code")
        meaning = _first_of(fields, "의미", "정식명", "meaning", "설명")
        if not code:
            return ""
        return f"{doc}에서 코드 또는 약어 '{code}'의 의미는 '{meaning}'이다."

    if ct == "note":
        note_text = _first_of(fields, "내용", "note", "주석") or (
            one_line(chunk.path[-1]) if chunk.path else ""
        )
        related = " > ".join(p for p in chunk.path[:-1] if one_line(p)) or title
        if not note_text:
            return ""
        return f"{doc}의 {related} 관련 주석: {note_text}"

    if ct == "total_row":
        subject = path_text or title
        sentences = _field_sentences(fields)
        base = f"{doc}의 {sheet} 시트에서 '{subject}' 합계 행이다."
        if sentences:
            base += f" {sentences}."
        return base

    if ct == "delegation_rule":
        subject = path_text or _first_of(fields, "항목") or title
        approvers = _stringify(fields.get("전결권자"))
        extras = []
        for key in ("합의", "수신"):
            value = _stringify(fields.get(key))
            if value:
                extras.append(f"{key}: {value}")
        base = f"{doc}의 {sheet} 시트에서 '{subject}' 항목"
        if approvers:
            text = f"{base}의 전결권자는 {approvers}이다."
        elif extras:
            text = f"{base}의 전결 관련 기준이다."
        else:
            return f"{base}에 대한 전결 기준 항목이다."
        if extras:
            text += " (" + ", ".join(extras) + ")"
        return text

    if ct == "unsupported_artifact":
        return f"{doc}의 {sheet} 시트 {chunk.range} 영역은 지원되지 않는 구조여서 원문 위치만 보존했다."

    # 알 수 없는 chunk_type — 보수적 기본 문장
    subject = path_text or title
    sentences = _field_sentences(fields)
    if sentences:
        return f"{doc}의 {sheet} 시트에서 '{subject}' 관련 항목: {sentences}."
    if subject:
        return f"{doc}의 {sheet} 시트에서 '{subject}' 관련 항목이다."
    return ""

