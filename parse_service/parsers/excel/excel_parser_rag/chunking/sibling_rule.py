"""sibling_rule — 계층 table_row 형제묶음 파생 청크 (사용자 설계 2026-07-13).

delegation_rule 이 matrix 행 위에 얹히는 것과 동형으로, 같은 (region_id, plain path 전체)
를 공유하는 **연속** table_row 들을 묶어 sibling_rule 파생 청크를 만든다. 원본 table_row 는
절대 제거하지 않는다(파생/additive) — 훅이 결과를 append 한다.

- 입력 필터: chunk_type=='table_row' 만(hierarchy_node/note/total 은 ' 항목:' 미포함이라
  헤더 분리가 오동작 → 명시 가드).
- 그룹 = (metadata["region_id"], plain path 튜플) 의 **연속 run**. 중간에 다른 path 행이
  끼면 별도 그룹으로 분리(sibling_merger packing 동형 — 비연속 동일 path 는 별도 그룹).
- **예외(spine_leaf 행, WBSID 같은 dotted-int 산):** 자기 leaf 를 path 끝에 얹으므로
  전 행 singleton 이 됨 → 부모(path[:-1]) 기준 **비연속 distinct-parent 버킷**으로 묶고
  자기 leaf 는 라인 kv 선두('- WBSID: 1.1.1, ...')에 보존(사용자 승인 2026-07-15).
  비-spine 행은 위 연속-run 정책 그대로(자산목록 보호) — 두 정책 한 리전 혼재 가능.
- content = 그룹 첫 행 content_text 의 ' 항목:' 앞부분(라벨 경로 헤더) 1줄 + 행당 '- {kv}' 라인.
  헤더 재사용은 table_row content 포맷에 결합 — 포맷 변경 시 동반 수정 필요.
- max_chars 초과 시 part 분할(hierarchy_pack 재사용, sibling_merger 동형).
- id = stable_id(source_file, sheet, "sibling_rule", part span, *path, str(part_index))
  — part 분할 시 part_index 로 유일성 보장.
- max_chars<=0 이면 미생성.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any, Dict, List, Tuple

from ..textutil import PARSER_VERSION, one_line, range_a1, stable_id
from .chunk_schema import RagChunk
from .hierarchy_pack import assign_parts, pack

_ITEM_SEP = " 항목:"


def _region_id(c: RagChunk) -> Any:
    return (c.metadata or {}).get("region_id")


def _is_spine_leaf(c: RagChunk) -> bool:
    """이 행이 자기 leaf(WBSID)를 path 끝에 얹은 spine 행인가 — 부모 그룹핑 대상.

    RagChunk 는 dataclass 라 (c.metadata or {}).get(...) 로 접근(dict .get 금지: AttributeError).
    """
    return bool((c.metadata or {}).get("spine_leaf"))


def _excel_row(c: RagChunk) -> int:
    md = c.metadata or {}
    src = c.source or {}
    v = md.get("excel_row")
    if isinstance(v, int):
        return v
    v = src.get("start_row")
    return v if isinstance(v, int) else 0


def _plain_path(c: RagChunk) -> Tuple[str, ...]:
    return tuple(one_line(p) for p in (c.path or []))


def _group_key(c: RagChunk) -> Tuple[Any, Tuple[str, ...]]:
    return (_region_id(c), _plain_path(c))


def _content_head(c: RagChunk) -> str:
    """행 content 의 ' 항목:' 앞 경로부(라벨 경로 프레임)."""
    return one_line(c.content_text).split(_ITEM_SEP, 1)[0]


def _row_kv(c: RagChunk) -> str:
    """행 content 의 ' 항목:' 뒷부분(kv)."""
    parts = one_line(c.content_text).split(_ITEM_SEP, 1)
    return parts[1].strip() if len(parts) > 1 else one_line(c.content_text).strip()


def _leaf_seg(c: RagChunk) -> str:
    """content 경로부의 마지막 ' > ' 세그먼트 (예: 'WBSID: 1.1.1') — 라벨 포함, 렌더 일관."""
    return _content_head(c).rsplit(" > ", 1)[-1]


def _header(first: RagChunk) -> str:
    """그룹 첫 행 content 의 ' 항목:' 앞부분 + ' 항목:' (라벨 경로 프레임 재사용)."""
    return f"{_content_head(first)}{_ITEM_SEP}"


def _spine_header(first: RagChunk) -> str:
    """spine 그룹 헤더 — 첫 멤버 경로부에서 마지막(자기 leaf) 세그먼트를 뗀 부모 체인 + ' 항목:'."""
    head = _content_head(first)
    parent = head.rsplit(" > ", 1)[0] if " > " in head else head
    return f"{parent}{_ITEM_SEP}"


def _row_line(c: RagChunk) -> str:
    """행 content 의 ' 항목:' 뒷부분(kv)을 '- {kv}' 라인으로."""
    return f"- {_row_kv(c)}"


def _spine_row_line(c: RagChunk) -> str:
    """spine 행 라인 — 자기 leaf(WBSID) 세그먼트를 kv 선두에 보존: '- WBSID: 1.1.1, {kv}'."""
    return f"- {_leaf_seg(c)}, {_row_kv(c)}"


def _compose(group: List[RagChunk]) -> str:
    # spine 그룹은 부모까지만 헤더로 두고 자기 leaf 를 라인에 얹는다(형제 묶음).
    if group and _is_spine_leaf(group[0]):
        return _spine_header(group[0]) + "\n" + "\n".join(_spine_row_line(c) for c in group)
    return _header(group[0]) + "\n" + "\n".join(_row_line(c) for c in group)


def _member_id(c: RagChunk) -> str:
    """멤버 table_row 의 finalize 후 id 를 재계산한다(chunk_factory.finalize_chunk 동일식).

    빌더는 parser.parse 내부에서 도는데 그 시점 멤버 RagChunk.id 는 아직 "" 이다(id 는
    pipeline 의 finalize_chunk 가 parse 반환 후 stable_id 로 부여). 따라서 c.id 직접 읽기 금지 —
    finalize 와 같은 식 stable_id(source_file, sheet, "table_row", range, *path) 로 재계산해
    member_ids 에 저장한다(new_chunk 가 source_file/sheet/range 를 이미 채웠고 finalize 가 그것을
    바꾸지 않으므로 재계산값 == 실 id). path 세그는 stable_id 내부에서 one_line 되므로 plain 동일.
    """
    return stable_id(c.source_file, c.sheet, "table_row", c.range, *_plain_path(c))


def _build_sibling(
    group: List[RagChunk], part_index: int, part_total: int, group_total: int
) -> RagChunk:
    first = group[0]
    # spine 그룹: 그룹 키(=.path/경로/id)는 부모까지(자기 leaf 제외). 비-spine 은 전체 path 유지.
    if _is_spine_leaf(first):
        path = list(_plain_path(first))[:-1]
    else:
        path = list(_plain_path(first))
    rows = [_excel_row(c) for c in group]
    cols = [(c.source or {}) for c in group]
    start_col = min((s.get("start_col") for s in cols if isinstance(s.get("start_col"), int)), default=None)
    end_col = max((s.get("end_col") for s in cols if isinstance(s.get("end_col"), int)), default=None)
    # source range = 멤버 행 min-max. spine 비연속 버킷은 사이에 낀 자손 행이 범위에 들 수 있으나
    # 자손은 자기 부모 버킷에 별도 존재(정보 손실 없음).
    r0, r1 = min(rows), max(rows)
    rng = range_a1(r0, start_col, r1, end_col) if (start_col and end_col) else (first.range or "")

    facts: List[Dict[str, Any]] = []
    for c in group:
        # subject = 각 멤버 자기 leaf(부모 아님) — spine 부모 그룹핑에서도 멤버별 자기 leaf 유지.
        member_path = _plain_path(c)
        leaf = member_path[-1] if member_path else ""
        for f in (c.facts or []):
            nf = dict(f)
            nf.setdefault("subject", leaf)
            facts.append(nf)

    confs = [(c.quality or {}).get("confidence") for c in group]
    confs = [x for x in confs if isinstance(x, (int, float))]
    quality = {
        "confidence": round(min(confs), 4) if confs else 0.0,
        "review_required": any((c.quality or {}).get("review_required") for c in group),
        "parser_version": PARSER_VERSION,
    }

    new_id = stable_id(first.source_file, first.sheet, "sibling_rule", rng, *path, str(part_index))
    return RagChunk(
        id=new_id,
        source_file=first.source_file,
        sheet=first.sheet,
        range=rng,
        chunk_type="sibling_rule",
        # region_type 은 그룹 첫 행 그대로 유지(delegation_rule._make_rule_chunk 미러 —
        # finalize_chunk 가 어차피 backfill 하나 단위테스트 경로 정합용 belt-and-suspenders).
        region_type=first.region_type,
        title=first.title,
        path=path,
        fields={
            "경로": " > ".join(path),
            "행수": len(group),        # 이 part 의 실제 라인 수
            "그룹행수": group_total,   # 그룹 총 행수(part 분할 시 구분)
        },
        facts=facts,
        content_text=_compose(group),
        source={
            "file": first.source_file, "sheet": first.sheet, "range": rng,
            "start_row": r0, "end_row": r1, "start_col": start_col, "end_col": end_col,
        },
        metadata={
            "region_id": _region_id(first),
            "derived_from": "table_row",
            "part_index": part_index,
            "part_total": part_total,
            # 대체용 — 이 part 멤버들의 finalize 후 실 id(재계산). additive(기존 키 불변).
            "member_ids": [_member_id(c) for c in group],
        },
        quality=quality,
    )


def build_sibling_rules(chunks: List[RagChunk], max_chars: int) -> List[RagChunk]:
    """table_row 들을 (region_id, plain path) 연속 run 단위로 묶어 sibling_rule 파생 청크 반환.

    입력을 변형하지 않는다(원본 table_row 보존). max_chars<=0 이면 [] 반환(비활성).
    """
    if max_chars <= 0:
        return []

    rows = [c for c in chunks if c.chunk_type == "table_row"]
    rows.sort(key=_excel_row)  # excel row 순 안정 정렬(입력이 이미 행순이어도 안전망)

    # 정책 분기(사용자 승인):
    #   - spine_leaf 행: 비연속 distinct-parent 버킷 (group_path = tuple(path[:-1])).
    #     DFS 순서상 형제 사이에 자손 행이 끼므로 연속-run 으로는 부모가 파편화 → 부모 키로 전 리전 누적.
    #     버킷 순서 = 부모 첫 등장 순(OrderedDict), 버킷 내 행 = excel row 순.
    #   - 비-spine 행: 기존 연속-run 유지 (자산목록의 비연속 동일-path 분리 보호). 두 정책 혼재 가능.
    groups: List[List[RagChunk]] = []
    spine_buckets: "OrderedDict[Tuple[Any, Tuple[str, ...]], List[RagChunk]]" = OrderedDict()
    run: List[RagChunk] = []

    def flush_run() -> None:
        if run:
            groups.append(list(run))
            run.clear()

    for c in rows:
        if _is_spine_leaf(c):
            flush_run()  # 비-spine 연속 run 경계
            key = (_region_id(c), tuple(_plain_path(c)[:-1]))
            spine_buckets.setdefault(key, []).append(c)
        elif run and _group_key(run[-1]) == _group_key(c):
            run.append(c)
        else:
            flush_run()
            run.append(c)
    flush_run()
    groups.extend(spine_buckets.values())  # 부모 첫 등장 순

    out: List[RagChunk] = []
    for group in groups:
        group_total = len(group)
        subgroups = pack(list(group), measure=lambda g: len(_compose(g)), max_chars=max_chars)
        for g, part_index, part_total in assign_parts(subgroups, multi_only=False):
            out.append(_build_sibling(g, part_index, part_total, group_total))
    return out


# ===========================================================================
# flat 형제묶음 빌더 (사용자 승인 2026-07-17 — 신WBS 이력/휴일목록/표지 KB 소실 fix)
# ---------------------------------------------------------------------------
# FlatTableParser 는 계층열이 없는 단순 표를 행마다 table_row(path=[title, leaf])로 낸다.
# auto openpyxl-hierarchy 프로파일이 sibling_rule 만 노출하면 이 flat 리전 시트(이력·휴일목록·
# 표지)가 통째 KB 에서 소실된다. 여기서 flat table_row 를 (region_id, 부모=path[:-1]) 연속 run
# 으로 묶어 sibling_rule 파생을 만든다(원본 table_row 는 보존 — additive, 훅이 append).
#
# 계층 build_sibling_rules 와의 차이(의도적):
#  - spine 분기 없음(flat 은 spine_leaf 무마킹).
#  - 헤더 = 부모 체인(자기 leaf 세그 뗌), 라인 = '- {kv}'(leaf-prefix 금지 — leaf 는 kv 선두에
#    이미 포함), 청크 path/경로 = 부모(title).
#  - 리전 게이트: (a) leaf 유일 비율 ≥ 0.9(정당 중복 구제/번호재시작 오염 차단),
#    (b) 행 kv부 평균 길이 ≤ max_chars//2(part 당 1행이면 묶음 무의미) — 실패 리전 통째 skip.
#  - metadata["flat_sibling"]=True(auto Tier2' 라우팅 신호 제외 — 계층-유래만 수락) +
#    member_ids(대체용, finalize id 재계산).
# ===========================================================================

_FLAT_LEAF_UNIQ_MIN = 0.9  # 리전 leaf 유일 비율 하한(distinct/total)


def _flat_header(first: RagChunk) -> str:
    """flat 그룹 헤더 — 첫 멤버 경로부에서 마지막(자기 leaf) 세그를 뗀 부모 체인 + ' 항목:'."""
    head = _content_head(first)
    parent = head.rsplit(" > ", 1)[0] if " > " in head else head
    return f"{parent}{_ITEM_SEP}"


def _flat_compose(group: List[RagChunk]) -> str:
    """부모 체인 헤더 1줄 + 행당 '- {kv}' 라인(leaf-prefix 금지 — leaf 는 kv 선두에 이미 있음)."""
    return _flat_header(group[0]) + "\n" + "\n".join(_row_line(c) for c in group)


def _build_flat_sibling(
    group: List[RagChunk], part_index: int, part_total: int, group_total: int
) -> RagChunk:
    first = group[0]
    # 청크 path/경로/id = 부모(자기 leaf 제외). flat path 는 [title, leaf] 2세그.
    path = list(_plain_path(first))[:-1]
    rows = [_excel_row(c) for c in group]
    cols = [(c.source or {}) for c in group]
    start_col = min((s.get("start_col") for s in cols if isinstance(s.get("start_col"), int)), default=None)
    end_col = max((s.get("end_col") for s in cols if isinstance(s.get("end_col"), int)), default=None)
    r0, r1 = min(rows), max(rows)
    rng = range_a1(r0, start_col, r1, end_col) if (start_col and end_col) else (first.range or "")

    facts: List[Dict[str, Any]] = []
    for c in group:
        member_path = _plain_path(c)
        leaf = member_path[-1] if member_path else ""  # subject = 각 멤버 자기 leaf
        for f in (c.facts or []):
            nf = dict(f)
            nf.setdefault("subject", leaf)
            facts.append(nf)

    confs = [(c.quality or {}).get("confidence") for c in group]
    confs = [x for x in confs if isinstance(x, (int, float))]
    quality = {
        "confidence": round(min(confs), 4) if confs else 0.0,
        "review_required": any((c.quality or {}).get("review_required") for c in group),
        "parser_version": PARSER_VERSION,
    }

    new_id = stable_id(first.source_file, first.sheet, "sibling_rule", rng, *path, str(part_index))
    return RagChunk(
        id=new_id,
        source_file=first.source_file,
        sheet=first.sheet,
        range=rng,
        chunk_type="sibling_rule",  # ★고정(CHUNK_TYPES 밖 신규 타입 금지 — 프로파일 누출 방지)
        region_type=first.region_type,
        title=first.title,
        path=path,
        fields={
            "경로": " > ".join(path),
            "행수": len(group),
            "그룹행수": group_total,
        },
        facts=facts,
        content_text=_flat_compose(group),
        source={
            "file": first.source_file, "sheet": first.sheet, "range": rng,
            "start_row": r0, "end_row": r1, "start_col": start_col, "end_col": end_col,
        },
        metadata={
            "region_id": _region_id(first),
            "derived_from": "table_row",
            "part_index": part_index,
            "part_total": part_total,
            "flat_sibling": True,                        # auto 라우팅 신호 제외용
            "member_ids": [_member_id(c) for c in group],  # 대체용(finalize id 재계산)
        },
        quality=quality,
    )


def _flat_region_passes(region_rows: List[RagChunk], cap_half: int) -> bool:
    """리전 게이트 — leaf 유일 비율 ≥ 0.9 ∧ 행 kv부 평균 길이 ≤ max_chars//2."""
    total = len(region_rows)
    if total == 0:
        return False
    leaves = []
    for c in region_rows:
        p = _plain_path(c)
        leaves.append(p[-1] if p else "")
    if len(set(leaves)) / total < _FLAT_LEAF_UNIQ_MIN:
        return False
    avg_kv = sum(len(_row_kv(c)) for c in region_rows) / total
    if avg_kv > cap_half:
        return False
    return True


def build_flat_siblings(chunks: List[RagChunk], max_chars: int) -> List[RagChunk]:
    """flat table_row 를 (region_id, 부모=path[:-1]) 연속 run 으로 묶어 sibling_rule 파생 반환.

    입력을 변형하지 않는다(원본 table_row 보존, additive). max_chars<=0 이면 [] 반환(비활성).
    리전 게이트(유일 비율/크기) 실패 리전은 통째 skip(낱개 유지), 싱글턴 run 은 emit 안 함.
    """
    if max_chars <= 0:
        return []

    rows = [c for c in chunks if c.chunk_type == "table_row"]
    if not rows:
        return []

    cap_half = max_chars // 2
    by_region: "OrderedDict[Any, List[RagChunk]]" = OrderedDict()
    for c in rows:
        by_region.setdefault(_region_id(c), []).append(c)

    out: List[RagChunk] = []
    for region_rows in by_region.values():
        if not _flat_region_passes(region_rows, cap_half):
            continue
        region_rows = sorted(region_rows, key=_excel_row)  # excel row 순(연속 run 판정)
        groups: List[List[RagChunk]] = []
        run: List[RagChunk] = []
        run_key: Any = None
        for c in region_rows:
            key = tuple(_plain_path(c)[:-1])  # 부모(title) 키
            if run and key == run_key:
                run.append(c)
            else:
                if run:
                    groups.append(run)
                run = [c]
                run_key = key
        if run:
            groups.append(run)

        for group in groups:
            if len(group) < 2:  # 싱글턴 skip(2행+ 만)
                continue
            group_total = len(group)
            subgroups = pack(list(group), measure=lambda g: len(_flat_compose(g)), max_chars=max_chars)
            for g, part_index, part_total in assign_parts(subgroups, multi_only=False):
                out.append(_build_flat_sibling(g, part_index, part_total, group_total))
    return out


# ===========================================================================
# kordoc 전용 dict 빌더 (대체 b안 — 사용자 승인 2026-07-15)
# ---------------------------------------------------------------------------
# kordoc 백엔드는 dict 청크를 다룬다(RagChunk 아님) — Counter(c['chunk_type']) 등
# subscript 접근이라 위 RagChunk 빌더 재사용 불가. 그룹핑/part 분할 알고리즘만 공유하고
# 별도 dict in/out 빌더를 둔다. openpyxl 경로(build_sibling_rules)는 그대로 존치.
#
# openpyxl 과의 차이(의도적):
#  - 그룹 키 = (sheet, plain path) **연속 run** (리스트 순). _group_key(=region_id,path)
#    재사용 금지: kordoc 메타에 region_id 없어 (None, path) 로 시트 경계를 넘어 오병합.
#  - spine_leaf 분기 없음(kordoc 은 그 신호 미부여 — 별도 함수라 자연 배제).
#  - metadata["section"] 게이트 + start_row(int) 게이트.
#  - 신형은 metadata.content_frame exact prefix, legacy만 ' 항목:' 분리 게이트.
#  - coord-unresolved(start_row 비int) 행은 run 참가 금지 → run 중간이면 run 분리(split).
# ===========================================================================

_SIBLING_RUN_MAX = 30  # 이 행수 초과 run 은 묶지 않음(표 전체 성격 — 메가런 가드)

# ---------------------------------------------------------------------------
# 메타 시트 통묶음 (사용자 승인 2026-07-20 — 표지/이력/변경기록/현황 시트당 1묶음)
# ---------------------------------------------------------------------------
# 사용자 판단: "현황·변경이력 같은 시트는 실제 검색할 데이터가 아님 — 그런 시트는 하나로 묶자."
# 메타 시트는 핀포인트 검색 수요가 없어 묶음 비용(핀포인트 희석)이 소멸하고 이득(인덱스 노이즈
# 감소·검색 슬롯 절약)만 남는다. section 게이트는 데이터 시트에 대해 유지(오묶음 방어).
#
# 판정: 시트명 **정규화(공백/_/- 제거, lower) exact 매칭**. contains 금지 —
#   '외부데이터소스 현황'·'접근제어 현황'(데이터 시트) 오탐이 나기 때문(실측). exact 라 자연 배제.
# '현황' 시트는 순수 boilerplate 가 아니라 수치 요약(총자산수 74 등)이나, 사용자 명시 판단
#   ('검색할 데이터 아님')에 근거해 포함. 향후 현황 핀포인트 수요 확인 시 아래 어휘에서 '현황'
#   제거로 즉시 롤백 가능. 새 메타 시트명(예: 'Rev History' 변형)은 정규화 exact 한계상 미탐 —
#   낱개 유지(소실 아님, 안전 방향).
_META_SHEET_NAMES = {"표지", "이력", "변경이력", "변경기록", "개정이력", "현황",
                     "cover", "history", "revision"}


def _is_meta_sheet(sheet: str) -> bool:
    """정규화 exact 매칭 — contains 는 '외부데이터소스 현황' 류 데이터 시트 오탐(실측)이라 금지."""
    return re.sub(r"[\s_\-]+", "", sheet or "").lower() in _META_SHEET_NAMES


def _kd_plain_path(c: Dict[str, Any]) -> Tuple[str, ...]:
    return tuple(one_line(p) for p in (c.get("path") or []))


def _kd_eligible(c: Dict[str, Any]) -> bool:
    """sibling run 참가 자격.

    메타 시트(_is_meta_sheet)의 행은 section 게이트를 면제한다(시트 통묶음 — section 없이도 eligible).
    신형 Kordoc 청크는 metadata.content_frame 과 exact prefix를 구조 기준으로
    사용한다. content_frame이 없는 legacy 청크만 구분자 split으로 복구한다.
    """
    if c.get("chunk_type") != "table_row":
        return False
    if not _is_meta_sheet(c.get("sheet")) and not (c.get("metadata") or {}).get("section"):
        return False
    src = c.get("source") or {}
    if not isinstance(src.get("start_row"), int):  # coord-unresolved 배제(start_row=None 붕괴 방지)
        return False
    ct = one_line(c.get("content_text") or "")
    md = c.get("metadata") or {}
    if "content_frame" in md:
        frame = one_line(md.get("content_frame"))
        prefix = f"{frame}{_ITEM_SEP}"
        return bool(frame and ct.startswith(prefix) and ct[len(prefix):].strip())
    parts = ct.split(_ITEM_SEP, 1)
    return len(parts) > 1 and bool(parts[1].strip())


def _kd_head(c: Dict[str, Any]) -> str:
    md = c.get("metadata") or {}
    if "content_frame" in md:
        return one_line(md.get("content_frame"))
    return one_line(c.get("content_text") or "").split(_ITEM_SEP, 1)[0]


def _kd_kv(c: Dict[str, Any]) -> str:
    ct = one_line(c.get("content_text") or "")
    md = c.get("metadata") or {}
    if "content_frame" in md:
        prefix = f"{one_line(md.get('content_frame'))}{_ITEM_SEP}"
        return ct[len(prefix):].strip() if ct.startswith(prefix) else ""
    parts = ct.split(_ITEM_SEP, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _kd_compose(group: List[Dict[str, Any]]) -> str:
    """첫 행 ' 항목:' 앞 경로부(라벨 프레임) 헤더 1줄 + 행당 '- {kv}' 라인."""
    header = f"{_kd_head(group[0])}{_ITEM_SEP}"
    return header + "\n" + "\n".join(f"- {_kd_kv(c)}" for c in group)


def _build_kordoc_sibling(
    group: List[Dict[str, Any]], part_index: int, part_total: int, group_total: int
) -> Dict[str, Any]:
    first = group[0]
    doc = first.get("source_file")
    sheet = first.get("sheet")
    path = list(_kd_plain_path(first))
    md0 = first.get("metadata") or {}

    rows = [(c.get("source") or {}).get("start_row") for c in group]  # 전원 int(eligible 보장)
    cols = [(c.get("source") or {}) for c in group]
    start_col = min((s.get("start_col") for s in cols if isinstance(s.get("start_col"), int)), default=None)
    end_col = max((s.get("end_col") for s in cols if isinstance(s.get("end_col"), int)), default=None)
    r0, r1 = min(rows), max(rows)
    if start_col and end_col:
        rng = range_a1(r0, start_col, r1, end_col)
    else:
        rng = first.get("range") or f"A{r0}:A{r1}"

    facts: List[Dict[str, Any]] = []
    for c in group:
        for f in (c.get("facts") or []):
            facts.append(dict(f))

    keywords: List[str] = []
    seen: set = set()
    for c in group:
        for k in (c.get("keywords") or []):
            if k not in seen:
                seen.add(k)
                keywords.append(k)
    keywords = keywords[:30]

    # core_text/embedding_text = 멤버 core_text 라인 합산(KB payload 품질 — 비우면 검색 메타 저하).
    cores = [(c.get("metadata") or {}).get("core_text") or "" for c in group]
    core_joined = "\n".join(x for x in cores if x)

    confs = [(c.get("quality") or {}).get("confidence") for c in group]
    confs = [x for x in confs if isinstance(x, (int, float))]
    quality = {
        "confidence": round(min(confs), 2) if confs else 0.0,
        "review_required": any((c.get("quality") or {}).get("review_required") for c in group),
        "parser_version": PARSER_VERSION,
    }

    new_id = stable_id(doc, sheet, "sibling_rule", rng, *path, str(part_index))
    return {
        "id": new_id,
        "source_file": doc,
        "sheet": sheet,
        "range": rng,
        "chunk_type": "sibling_rule",
        "region_type": first.get("region_type"),
        "title": first.get("title"),
        "path": path,
        "fields": {"경로": " > ".join(path), "행수": len(group), "그룹행수": group_total},
        "facts": facts,
        "content_text": _kd_compose(group),
        "keywords": keywords,
        "source": {
            "file": doc, "sheet": sheet, "range": rng,
            "start_row": r0, "end_row": r1, "start_col": start_col, "end_col": end_col,
        },
        # ★metadata["merged"] 설정 금지(sibling_merger._build_merged 복사 함정 회피).
        "metadata": {
            "workbook_title": md0.get("workbook_title"),
            "section": md0.get("section"),
            "content_frame": _kd_head(first),
            "core_text": core_joined,
            "embedding_text": core_joined,
            "derived_from": "table_row",
            "part_index": part_index,
            "part_total": part_total,
            # 문서 순서 재배치용(사용자 요청) — 이 part 멤버들의 파이썬 object identity(id()).
            # ⚠️ stable_id/문자열 id 대조 금지: kordoc dict id 는 좌표 placeholder 라 0건 매칭 =
            # 데이터 소실. replaced set 과 동일 체계(id()). 훅 재배치가 앵커 산출 후 pop 한다(KB 유출 방지).
            "member_obj_ids": [id(m) for m in group],
        },
        "quality": quality,
    }


def _reorder_kordoc_siblings(
    all_chunks: List[Dict[str, Any]],
    siblings: List[Dict[str, Any]],
    replaced: set,
) -> List[Dict[str, Any]]:
    """kordoc sibling 을 문서 순서 위치(첫 멤버 원본 자리)에 삽입해 새 청크 리스트 반환.

    사용자 요청("kb 로 들어가는 경우 … 관련있는 순서 Order 를 정해서 들어가야"). 기존 훅은
    sibling 을 리스트 말미에 일괄 append 해 문서 순서를 파괴했다(감사팀 요약 sibling 이 타 시트
    낱개들 뒤). 각 sibling 은 자기 멤버 중 **첫 멤버**(all_chunks 상 최소 위치)의 자리에 앵커한다.

    - part 분할 sibling 들은 각자 자기 첫-멤버 위치에 앵커된다(멤버 집합 서로소). '연속'을 강제하지
      않음 — 사이 원본이 대체-제거되어 연속으로 창발한다.
    - member_obj_ids 는 파이썬 object identity(id()) — replaced set 과 동일 체계. 빌더 호출과
      본 재배치가 같은 parse 패스 안이라 identity 가 유효하다.
    - 앵커 산출 후 metadata 에서 pop 한다(좌표 placeholder id 유출 방지 — KB 오염 차단).
    - 빈 mids(이론상 도달 불가) 방어: 앵커 못 구하면 말미(len(all_chunks))로 밀어 데이터 보존.
    - 집합 등가: 산출 청크 집합(내용·개수·id)은 완전 동일, 순서만 변경.
    """
    pos_of = {id(c): i for i, c in enumerate(all_chunks)}
    at: Dict[int, List[Dict[str, Any]]] = {}   # 앵커 위치 -> [sibling,...] (같은 위치는 buildorder)
    for s in siblings:
        mids = (s.get("metadata") or {}).pop("member_obj_ids", [])   # pop — KB 유출 방지
        anchor = min((pos_of[m] for m in mids if m in pos_of), default=len(all_chunks))
        at.setdefault(anchor, []).append(s)
    out: List[Dict[str, Any]] = []
    for i, c in enumerate(all_chunks):
        out.extend(at.get(i, []))
        if id(c) not in replaced:
            out.append(c)
    out.extend(at.get(len(all_chunks), []))   # 빈 mids 방어 앵커(도달 불가나 데이터 보존)
    return out


def build_kordoc_siblings(
    chunks: List[Dict[str, Any]], max_chars: int, run_max: int = _SIBLING_RUN_MAX
) -> Tuple[List[Dict[str, Any]], set]:
    """kordoc dict 청크에서 (sheet, path) 연속 run 을 sibling_rule 로 묶어 (siblings, replaced).

    - replaced = 묶인 원본 청크의 object identity(id()) 집합 — 호출측이 이 id 를 제거(대체 b안).
    - max_chars<=0 이면 ([], set()) — 비활성(양 백엔드 공통 토글 sibling_rule_max_chars 공유).
    - 2행+ 만 묶고, len(run) > run_max 는 skip(원본 유지 — 메가런 가드).
    - eligible 아닌 행(비-table_row/section 無/coord-unresolved/frame 검증 실패)은 run 경계(split).

    **메타 시트 통묶음(사용자 승인 2026-07-20):** _is_meta_sheet 시트의 eligible 행은
      section 게이트·run_max 를 면제받고, **연속-run 이 아니라 시트 단위 dict 누적**(key=(sheet,))
      으로 묶인다. 중간에 non-eligible 행(start_row=None·' 항목:' 분리 실패)이 껴도 버킷은 유지되고
      그 행만 낱개 잔존(dict 누적 방식 — 연속-run 이면 그 지점에서 갈라짐). 비-메타 시트는 완전 무변경.
    """
    if max_chars <= 0:
        return [], set()

    runs: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    cur_key: Any = None
    # 메타 시트 버킷 — 시트 단위 dict 누적(연속-run 아님). key=(sheet,) 통합 의미론.
    #   실코퍼스 메타 시트는 시트당 단일 path 라 실질 동작은 연속-run 과 동일하나, 경로 상이
    #   이론 케이스에서도 (sheet,) 로 1버킷 유지(시트 통묶음 보장).
    meta_buckets: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()

    def flush_cur() -> None:
        nonlocal cur, cur_key
        if cur:
            runs.append(cur)
            cur = []
        cur_key = None

    for c in chunks:
        if not _kd_eligible(c):
            flush_cur()  # non-eligible 은 비-메타 연속 run 경계. 메타 버킷은 유지(dict 누적).
            continue
        if _is_meta_sheet(c.get("sheet")):
            flush_cur()  # 메타 행은 비-메타 연속 run 에 끼어들지 않음
            meta_buckets.setdefault(c.get("sheet"), []).append(c)
            continue
        key = (c.get("sheet"), _kd_plain_path(c))
        if cur and key == cur_key:
            cur.append(c)
        else:
            flush_cur()
            cur.append(c)
            cur_key = key
    flush_cur()

    siblings: List[Dict[str, Any]] = []
    replaced: set = set()

    def _emit(group: List[Dict[str, Any]]) -> None:
        subgroups = pack(list(group), measure=lambda g: len(_kd_compose(g)), max_chars=max_chars)
        # 한 행만으로 cap을 넘는 원자 청크는 자르거나 파생 청크로 바꾸지 않는다.
        mergeable = [g for g in subgroups if len(g) >= 2]
        group_total = sum(len(g) for g in mergeable)
        for g, part_index, part_total in assign_parts(mergeable, multi_only=False):
            siblings.append(_build_kordoc_sibling(g, part_index, part_total, group_total))
            for c in g:
                replaced.add(id(c))

    for run in runs:
        if len(run) < 2:            # 싱글턴 무대체(2행+ 만)
            continue
        if len(run) > run_max:      # 메가런 skip(원본 유지)
            continue
        _emit(run)

    # 메타 시트 버킷 — run_max 면제(시트 전체를 통묶음). 2행+ 만, part 분할(cap)은 그대로.
    for bucket in meta_buckets.values():
        if len(bucket) < 2:         # 1행 시트는 낱개 유지(묶을 게 없음)
            continue
        _emit(bucket)

    return siblings, replaced
