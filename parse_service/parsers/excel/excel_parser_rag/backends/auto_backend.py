"""AutoBackend — 전결/계층 문서를 openpyxl 로 라우팅하는 3-tier 라우터.

Tier1: 헤더 영역에 "전결" 토큰이 있으면 후보 → openpyxl(delegation) 시도.
Tier2: openpyxl 결과에 delegation_rule 이 1개 이상이면 채택, 아니면 kordoc fallback.
Tier1.5(계층): "전결" 키워드 없음 + .xlsx/.xlsm → 구조 프로브(hierarchy_dominance).
  hierarchical_table(계층열 보유) 행 지배도 ≥ _HIER_DOMINANCE_MIN → openpyxl(hierarchy) 시도.
Tier2'(계층 수락): sibling_rule 이 1개 이상이면 채택(routed_profile="hierarchy"),
  아니면 kordoc fallback (지배도 우연 통과 방어 — '자산목록 6920 쓰레기' 방어선).
그 외/프로브 실패: kordoc (openpyxl 미접촉 → 평면목록 오분류 원천 차단).
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, List, Tuple

import openpyxl

from ..config import ParserConfig
from .base import Backend

_DELEGATION_TOKEN = "전결"
_OPENPYXL_PROFILES = ["delegation_rule", "note", "code_mapping"]
# 비전결 계층 문서(WBS류) openpyxl 라우팅 시 프로파일. note/code_mapping 은 정보 손실 방지용
# 보존(구WBS 코퍼스에선 note 0 인 dead entry 지만 유지), sibling_rule 이 계층 청크 산출축.
_HIERARCHY_PROFILES = ["sibling_rule", "note", "code_mapping", "table_row", "total_row"]
# Tier1.5 판정 임계 — hierarchical_table(계층열 보유) 행 지배도. 코퍼스 실측 여유(구WBS 0.835 vs 자산목록 0.051).
_HIER_DOMINANCE_MIN = 0.5
# openpyxl 직접 읽기가 가능한 확장자만 라우팅 대상.
# ⚠️ 2026-08-13 정정 — 예전 주석은 ".xls 는 소속 backend 가 soffice 변환을 내부 처리" 라고
# 적혀 있었으나 **사실이 아니었다**. 변환은 openpyxl 백엔드 경로에만 걸려 있었고 .xls 는
# 여기서 kordoc 으로 라우팅돼 그 변환을 한 번도 타지 못했다(→ kordoc 백엔드의 동반
# openpyxl 읽기에서 InvalidFileException, 그리고 전결 .xls 가 delegation_rule 을 못 만듦).
# 이제 **레인 입구**(parsers/excel/__init__.py)가 CFB 매직을 보고 .xlsx 로 갈아끼우므로
# .xls 는 이 지점에 도달하지 않는다.
_OPENPYXL_SUFFIXES = {".xlsx", ".xlsm"}


def hierarchy_dominance(input_path) -> float:
    """비전결 문서 구조 프로브 — hierarchical_table(계층열 보유) 리전의 행 지배도.

    build_canvases + detect_and_classify 만 수행(파싱 없음). 실패 시 0.0(→ kordoc, 안전).
    비용 주의: 비전결 xlsx 는 워크북이 2회 open 됨(detect_delegation_keyword 스캔 + 여기 build_canvases).
    """
    from ..pipeline import build_canvases, detect_and_classify

    try:
        pairs = detect_and_classify(
            build_canvases(Path(input_path), ParserConfig()), ParserConfig()
        )
    except Exception:
        return 0.0  # 프로브 실패 → kordoc (안전)
    hier = total = 0
    for rg, _cv in pairs:
        n = rg.row_count  # Region.row_count 프로퍼티(detection/region.py) — 수동 산술 대신
        total += n
        if rg.region_type == "hierarchical_table" and rg.hierarchy_cols:
            hier += n
    return hier / total if total else 0.0


def detect_delegation_keyword(input_path, *, max_rows: int = 40, max_cols: int = 30) -> bool:
    """워크북 상단 영역에 "전결" 토큰이 한 번이라도 나오면 True."""
    path = Path(input_path)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            for row in ws.iter_rows(min_row=1, max_row=max_rows, max_col=max_cols, values_only=True):
                for v in row:
                    if isinstance(v, str) and _DELEGATION_TOKEN in v:
                        return True
        return False
    finally:
        wb.close()


def _should_try_openpyxl(input_path) -> bool:
    """Tier1: openpyxl 가능 확장자(.xlsx/.xlsm) + "전결" 키워드일 때만 True.
    `and` 단락 평가로 .xls 는 detect 를 호출하지 않는다(파일 미존재여도 안전)."""
    return (
        Path(input_path).suffix.lower() in _OPENPYXL_SUFFIXES
        and detect_delegation_keyword(input_path)
    )


def _with(config: ParserConfig, backend: str, *, profiles=None) -> ParserConfig:
    cfg = copy.copy(config)
    cfg.backend = backend
    if profiles is not None:
        cfg.chunk_profiles = list(profiles)
    return cfg


class AutoBackend(Backend):
    name = "auto"

    def parse(self, input_path, config: ParserConfig) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        from . import get_backend  # 지연 import (순환 방지)

        def _kordoc():
            chunks, stats = get_backend("kordoc").parse(input_path, _with(config, "kordoc"))
            stats = dict(stats); stats["routed_backend"] = "kordoc"
            return chunks, stats

        # Tier1: "전결" 키워드(+대상 확장자) → openpyxl(delegation) 시도
        if _should_try_openpyxl(input_path):
            op_cfg = _with(config, "openpyxl", profiles=_OPENPYXL_PROFILES)
            chunks, stats = get_backend("openpyxl").parse(input_path, op_cfg)
            # Tier2: delegation_rule ≥1 → 채택
            if any(c.get("chunk_type") == "delegation_rule" for c in chunks):
                stats = dict(stats); stats["routed_backend"] = "openpyxl"
                return chunks, stats
            # 키워드만 우연히 박힌 문서 → kordoc fallback
            return _kordoc()

        # Tier1.5: 키워드 없음 + openpyxl 가능 확장자 → 구조 프로브(계층 지배도)
        # hierarchy_dominance 는 module-global bare 참조(monkeypatch 바인딩용 — 지역 alias 금지)
        if (
            Path(input_path).suffix.lower() in _OPENPYXL_SUFFIXES
            and hierarchy_dominance(input_path) >= _HIER_DOMINANCE_MIN
        ):
            op_cfg = _with(config, "openpyxl", profiles=_HIERARCHY_PROFILES)
            chunks, stats = get_backend("openpyxl").parse(input_path, op_cfg)
            # Tier2' 수락 신호 = 계층-유래 sibling_rule(flat 파생 제외 — '6920' 방어선 유지:
            # 팀별을 dominance 강제해도 flat sibling 으론 수락 안 됨).
            def _is_hier_sibling(c):
                return (
                    c.get("chunk_type") == "sibling_rule"
                    and not (c.get("metadata") or {}).get("flat_sibling")
                )

            if any(_is_hier_sibling(c) for c in chunks):
                # 대체(replaced) 범위 = flat 포함 전체 sibling 의 member_ids ⋃(수락 술어 재사용 금지 —
                # 재사용하면 flat 원본 table_row 가 잔존 중복). 채택된 sibling 이 묶은 원본 table_row 제거.
                replaced: set = set()
                for c in chunks:
                    if c.get("chunk_type") == "sibling_rule":
                        replaced.update((c.get("metadata") or {}).get("member_ids") or [])
                if replaced:
                    chunks = [
                        c for c in chunks
                        if not (c.get("chunk_type") == "table_row" and c.get("id") in replaced)
                    ]
                stats = dict(stats)
                stats["routed_backend"] = "openpyxl"
                stats["routed_profile"] = "hierarchy"
                return chunks, stats

        # 그 외(.xls·비계층·프로브 실패·sibling 0) → kordoc (현행 동작 보존)
        return _kordoc()
