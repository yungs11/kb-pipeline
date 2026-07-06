"""CGH (Cascade-Gated Hierarchy) 청킹 — 부모-자식 계층 보존 후처리.

계약: docs/specs/CGH-spec.md ★검증 계약(v2). merge_wbs_rows(Method B)를 대체.
모든 내부노드가 직속 자식 아웃라인을 품는 hierarchy_node 요약청크를 항상 발행한다.

캐스케이드 게이트: matrix_fact → 번호 spine(depth 분산 & 비율) → 없으면 no-op(원본 반환).
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from ..textutil import PARSER_VERSION
from .hierarchy_pack import assign_parts, pack

# ---------------------------------------------------------------- 정규식/상수
HANGUL_ORD = "가나다라마바사아자차카타파하"
_HORD = {ch: i + 1 for i, ch in enumerate(HANGUL_ORD)}
_SEG = re.compile(r"(\d+|[" + HANGUL_ORD + r"])")
_SEP = re.compile(r"\s*[.)．）]\s*|\s+")
_MARKER_PREFIX = re.compile(r"^\s*(?:(?:\d+|[" + HANGUL_ORD + r"])\s*[.)．）]\s*)+")

# 롤업/총계 라벨 (앵커드 — '설계'·'계획완료' 오탐 금지)
_ROLLUP = re.compile(r"(진척율|소계|중간합계|누계|총계|합계|total)\s*$", re.I)
_TOTAL = re.compile(r"^(ALL|전체)\b", re.I)
# 버전/개정 열 가드
_VERSION_KEY = re.compile(r"버전|version|\bver\.?\b|개정|리비전|revision|rev\.?", re.I)
# 단위 접미(소수/비율/수량) — norm_num 배제용
_UNIT_SUFFIX = re.compile(r"(억|원|시간|개|명|건|%|％)\s*$")


# ================================================================ norm_num (must-fix 1)
def norm_num(raw: Any, *, in_spine: bool = False) -> Optional[str]:
    """선두 계층 마커 → 정규화 dotted-int 문자열. 아라비아(1.1.1) + 한글서수(가.1).

    반환 None = 계층 마커 아님. 소수/비율/단위(0.30, 1.5, 30%, 5억)는 강배제.

    in_spine=False(기본, 엄격): 트레일링 라벨/구분자 없는 순수 2세그먼트 `d.d`(1.5, 3.5)
      는 소수로 간주 배제. detect_spine 게이팅·단위테스트가 사용.
    in_spine=True(완화): spine 열로 판정된 뒤 노드 번호 파싱용. 순수 `d.d`(1.1) 도 계층
      번호로 인정(단, 첫세그먼트 0/단위/퍼센트는 여전히 배제).
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    # must-fix 1: 트레일링 단위/퍼센트가 붙으면 계층 아님
    if _UNIT_SUFFIX.search(s):
        return None

    segs: List[int] = []
    is_arabic = False
    pos = 0
    _DELIM = ".)．)"                       # 세그먼트 종료 구분자
    while pos < len(s):
        m = _SEG.match(s, pos)
        if not m or m.start() != pos:
            break
        tok = m.group(1)
        end = m.end()
        nxt = s[end:end + 1]               # 세그먼트 직후 1글자
        # 한글 서수는 뒤에 구분자/공백/끝이 반드시 와야 인정('가능성'의 '가' 차단)
        if tok in _HORD and not (nxt == "" or nxt.isspace() or nxt in _DELIM):
            break
        if tok in _HORD:
            segs.append(_HORD[tok])
        else:
            segs.append(int(tok))
            is_arabic = True
        pos = end                          # 소비한 세그먼트 끝으로 전진(종료 시 remainder 계산 정확)
        # 다음 세그먼트로의 연속은 '공백 없는 tight dot' 일 때만.
        # (예: "1.1.1" 은 이어지고, "6.3. 1차" 의 ". 1" 은 라벨이므로 끊는다)
        if nxt == "." and _SEG.match(s, end + 1) and s[end + 1:end + 2] not in ("", " ", "\t"):
            pos = end + 1
            continue
        break
    if not segs:
        return None
    # must-fix 1: 엄격모드 — 라벨/구분자 없는 순수 단일소수 float(1.5/3.5/100.0) 배제.
    # 계층 dotted(1.1.1)은 점 2개↑ → 세그먼트 3개↑ 이므로 안전. 소수는 점 정확히 1개.
    if not in_spine and is_arabic and len(segs) == 2:
        consumed = s[:pos].strip()
        remainder = s[pos:].strip()
        if remainder == "" and re.fullmatch(r"\d+\.\d+", consumed):
            return None
    # 가드: 첫 세그먼트 0 (0.30 등) / 비정상 큰 값(>999)
    if segs[0] == 0:
        return None
    if any(x > 999 for x in segs):
        return None
    return ".".join(str(x) for x in segs)


def numbering_system_of(raw: Any) -> Optional[str]:
    """원문 마커가 arabic 인지 hangul 인지 판별. norm_num 이 None 이면 None."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s or _UNIT_SUFFIX.search(s):
        return None
    m = _SEG.match(s)
    if not m or m.start() != 0:
        return None
    tok = m.group(1)
    end = m.end()
    sep = _SEP.match(s, end)
    at_end = end == len(s)
    if tok in _HORD:
        return "hangul" if (sep or at_end) else None
    # arabic: norm_num 이 유효해야 arabic 으로 인정
    return "arabic" if norm_num(s) else None


def _num_key(s: str) -> List[int]:
    return [int(x) for x in s.split(".")]


# ================================================================ spine 감지 (must-fix 2,4)
def detect_spine(rows: List[Dict[str, Any]], *, min_ratio: float = 0.6) -> Optional[str]:
    """table_row 리스트 → 번호 spine 필드명 or None.

    게이트:
      - 정규화 번호 비율 ≥ min_ratio (분모 = strip 후 비어있지 않은 셀)
      - depth≥2 인 행이 서로 다른 2건 이상 (평면 ID열 + 단일 중첩행 오발화 차단)
      - 버전열 제외
      - 한 열에 arabic/hangul 혼용 충돌 시 폐기(None)
    """
    # field -> [match, nonempty, set(distinct深2번호), set(systems)]
    cand: Dict[str, Dict[str, Any]] = {}
    for c in rows:
        for k, v in (c.get("fields") or {}).items():
            if not isinstance(v, str) or not v.strip():
                continue
            slot = cand.setdefault(k, {"m": 0, "n": 0, "deep": set(),
                                       "sys": set(), "nums": set()})
            slot["n"] += 1
            nn = norm_num(v, in_spine=True)
            if nn:
                slot["m"] += 1
                slot["nums"].add(nn)
                if nn.count(".") >= 1:  # depth>=2
                    slot["deep"].add(nn)
                sysv = numbering_system_of(v)
                if sysv:
                    slot["sys"].add(sysv)
    best, best_m = None, -1
    for k, slot in cand.items():
        if _VERSION_KEY.search(str(k)):
            continue
        n, m = slot["n"], slot["m"]
        if n < 3:
            continue
        if m / n < min_ratio:
            continue
        # must-fix 2: depth>=2 인 서로 다른 번호가 2건 이상
        if len(slot["deep"]) < 2:
            continue
        # must-fix 4: 혼용 충돌 → 폐기
        if len(slot["sys"]) > 1:
            continue
        # IP/버전 오발화 차단: depth>=2 번호 중 관측된 부모(더 짧은 prefix)를 갖는
        # 비율이 낮으면 트리 아님(IP는 3-octet prefix 행이 없어 부모 0).
        allnums = slot["nums"]
        deep = slot["deep"]
        with_parent = sum(1 for x in deep if parent_of(x, allnums) is not None)
        if not deep or with_parent / len(deep) < 0.5:
            continue
        # 측정값 열(공수/금액) 오발화 차단: 진짜 계층은 '자식 2개 이상인 부모'가
        # 최소 1개 존재(분기). 고립된 소수(3.2 홀로, 100.2 합계)뿐인 열은 분기가 없어 배제.
        childcount: Dict[str, int] = {}
        for x in allnums:
            p = parent_of(x, allnums)
            if p is not None:
                childcount[p] = childcount.get(p, 0) + 1
        if not any(cnt >= 2 for cnt in childcount.values()):
            continue
        if m > best_m:
            best, best_m = k, m
    return best


# ================================================================ 트리 구성
def parent_of(num: str, allnums) -> Optional[str]:
    """관측된 최장 prefix 부모. 결번은 건너뛴다."""
    parts = num.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        p = ".".join(parts[:cut])
        if p in allnums:
            return p
    return None


def _label_of(chunk: Dict[str, Any], spine: str) -> str:
    """노드 라벨. spine 셀에서 마커 제거한 나머지 → 없으면 비-spine 서술 컬럼 첫 값."""
    f = chunk.get("fields") or {}
    sv = f.get(spine)
    if isinstance(sv, str):
        svs = sv.strip()
        # 순수 번호 마커 셀(WBSID '1.1', '가.1')이면 셀 내 라벨 없음 → 서술 컬럼으로 폴백.
        # (한글은 서수 14자만 마커로 취급; '차/입/찰' 같은 일반 음절이 있으면 라벨 있는 셀)
        pure = re.fullmatch(r"[\d" + HANGUL_ORD + r".)．)\s]+", svs)
        if not pure:
            rest = _MARKER_PREFIX.sub("", svs).lstrip(" .·)-").strip()
            if rest:
                return rest[:80]
    for k, v in f.items():
        if k == spine:
            continue
        if isinstance(v, str) and v.strip() and not norm_num(v, in_spine=True) \
                and not re.match(r"^-?[\d,.]+[%％]?$", v.strip()):
            return v.strip()[:80]
    return (sv or "").strip()[:80] if isinstance(sv, str) else ""


def _classify_rollup(chunk: Dict[str, Any], spine: str, num: Optional[str]) -> Optional[str]:
    """'total'(ALL/전체) / 'rollup'(합계/소계/진척율) / None."""
    f = chunk.get("fields") or {}
    raw = f.get(spine)
    if isinstance(raw, str) and _TOTAL.match(raw.strip()):
        return "total"
    for k, v in f.items():
        if isinstance(v, str) and _ROLLUP.search(v.strip()):
            return "rollup"
    return None


# ================================================================ 청크 빌더 (must-fix 7)
def _carry_meta(parent: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in (parent.get("metadata") or {}).items()
            if k in ("workbook_title", "section", "region_id", "sheet_index")}


def _coords(anchor: Dict[str, Any], last: Optional[Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    """부모행~마지막자식행 접합 range + source dict (_build_wbs 방식)."""
    a_src = anchor.get("source") or {}
    l_src = (last.get("source") or {}) if last else a_src
    r0 = a_src.get("start_row")
    r1 = l_src.get("end_row") or l_src.get("start_row") or r0
    prng = anchor.get("range") or ""
    crng = (last.get("range") if last else "") or prng
    if prng and crng and ":" in prng and ":" in crng:
        rng = prng.split(":")[0] + ":" + crng.split(":")[1]
    else:
        rng = prng or crng
    src_file = anchor.get("source_file", "")
    sheet = anchor.get("sheet", "")
    source = {"file": src_file, "sheet": sheet, "range": rng, "start_row": r0,
              "end_row": r1, "start_col": a_src.get("start_col"),
              "end_col": a_src.get("end_col")}
    return rng, source


def _build_node(*, anchor: Optional[Dict[str, Any]], node_no: str, label: str,
                breadcrumb: str, child_lines: List[str], child_nos: List[str],
                depth: int, spine_kind: str, numbering_system: str,
                part_index: int, part_total: int, sheet: str, src_file: str,
                confidence: float, review_required: bool, phantom: bool,
                last_child: Optional[Dict[str, Any]], roots: List[str],
                carry: Dict[str, Any]) -> Dict[str, Any]:
    head = f"[{sheet}] {node_no} {label}".rstrip()
    if breadcrumb:
        head += f"  (상위: {breadcrumb})"
    if part_total > 1:
        head += f"  (part {part_index}/{part_total})"
    content = head + ("\n" + "\n".join(child_lines) if child_lines else "")
    if phantom:
        content = f"[{sheet}] {node_no} (합성 상위노드)" \
                  + (f"  (part {part_index}/{part_total})" if part_total > 1 else "") \
                  + ("\n" + "\n".join(child_lines) if child_lines else "")

    if anchor is not None:
        rng, source = _coords(anchor, last_child)
        node_id = f"{src_file}::{sheet}::{rng}::hnode{node_no}p{part_index}"
    else:
        # 팬텀: 실제 행 없음 → 첫 자식 좌표 참조
        rng, source = _coords(last_child or {"source_file": src_file, "sheet": sheet},
                              last_child)
        if not rng:
            rng = f"phantom:{node_no}"
            source = {"file": src_file, "sheet": sheet, "range": rng,
                      "start_row": None, "end_row": None,
                      "start_col": None, "end_col": None}
        node_id = f"{src_file}::{sheet}::{rng}::hnodePH{node_no}p{part_index}"

    md = {**carry, "merged": True, "node_id": node_id, "node_no": node_no,
          "parent_no": parent_of_display(node_no, breadcrumb),
          "child_nos": child_nos, "depth": depth, "spine_kind": spine_kind,
          "numbering_system": numbering_system, "part_index": part_index,
          "part_total": part_total, "phantom": phantom, "breadcrumb": breadcrumb,
          "roots": roots, "core_text": content, "embedding_text": content}
    return {
        "id": node_id, "source_file": src_file, "sheet": sheet, "range": rng,
        "chunk_type": "hierarchy_node",
        "region_type": (anchor or {}).get("region_type", "") if anchor else "",
        "title": (anchor or {}).get("title") if anchor else sheet,
        "path": list((anchor or {}).get("path") or [sheet]),
        "fields": dict((anchor or {}).get("fields") or {}),
        "facts": list((anchor or {}).get("facts") or []),
        "content_text": content,
        "keywords": list((anchor or {}).get("keywords") or [])[:30],
        "source": source, "metadata": md,
        "quality": {"confidence": round(confidence, 4),
                    "review_required": bool(review_required),
                    "parser_version": PARSER_VERSION},
    }


def parent_of_display(node_no: str, breadcrumb: str) -> Optional[str]:
    """breadcrumb 마지막 조상을 parent_no 로. 없으면 None."""
    if not breadcrumb:
        return None
    last = breadcrumb.split(" > ")[-1]
    return last.split(":")[0].strip() or None


def _build_rollup(chunk: Dict[str, Any], cls: str, num: Optional[str], spine: str,
                  summarizes: List[str]) -> Dict[str, Any]:
    """롤업/총계 canonical 청크. total→total_row, rollup→section_summary."""
    src = chunk.get("source") or {}
    sheet = chunk.get("sheet", "")
    src_file = chunk.get("source_file", "")
    rng = chunk.get("range") or src.get("range") or ""
    f = chunk.get("fields") or {}
    label = _label_of(chunk, spine)
    parts = [f"{k}={v}" for k, v in f.items() if str(v).strip()]
    body = f"[{sheet}] (집계) {num or ''} {label}".strip() + "\n" + ", ".join(parts[:12])
    ctype = "total_row" if cls == "total" else "section_summary"
    node_id = f"{src_file}::{sheet}::{rng}::{ctype}{num or 'x'}"
    carry = _carry_meta(chunk)
    md = {**carry, "node_no": num, "summarizes": summarizes,
          "rollup_kind": cls, "core_text": body, "embedding_text": body}
    q = chunk.get("quality") or {}
    return {
        "id": node_id, "source_file": src_file, "sheet": sheet, "range": rng,
        "chunk_type": ctype, "region_type": chunk.get("region_type", ""),
        "title": chunk.get("title"), "path": list(chunk.get("path") or [sheet]),
        "fields": dict(f), "facts": list(chunk.get("facts") or []),
        "content_text": body, "keywords": list(chunk.get("keywords") or [])[:30],
        "source": {"file": src_file, "sheet": sheet, "range": rng,
                   "start_row": src.get("start_row"), "end_row": src.get("end_row"),
                   "start_col": src.get("start_col"), "end_col": src.get("end_col")},
        "metadata": md,
        "quality": {"confidence": q.get("confidence", 0.0) if isinstance(q.get("confidence"), (int, float)) else 0.0,
                    "review_required": bool(q.get("review_required")),
                    "parser_version": PARSER_VERSION},
    }


def _tag_leaf(chunk: Dict[str, Any], *, node_no: str, parent_no: Optional[str],
              depth: int, breadcrumb: str, spine_kind: str,
              numbering_system: str) -> Dict[str, Any]:
    """leaf = 기존 table_row shallow-copy + metadata 계층태그 in-place (must-fix 8)."""
    lf = dict(chunk)
    md = dict(chunk.get("metadata") or {})
    md.update({"node_no": node_no, "parent_no": parent_no, "depth": depth,
               "breadcrumb": breadcrumb, "spine_kind": spine_kind,
               "numbering_system": numbering_system, "merged": False})
    lf["metadata"] = md
    return lf


# ================================================================ 시트 처리
def _process_sheet(rows: List[Dict[str, Any]], all_chunks: List[Dict[str, Any]],
                   sheet: Any, *, max_chars: int, min_ratio: float
                   ) -> Optional[List[Dict[str, Any]]]:
    """시트의 table_row 를 CGH 처리. 발화 못하면 None(호출자가 원본 유지)."""
    # matrix 게이트
    if any(c.get("chunk_type") == "matrix_fact" and c.get("sheet") == sheet
           for c in all_chunks):
        return None
    spine = detect_spine(rows, min_ratio=min_ratio)
    if not spine:
        return None

    # 시트 전체의 numbering_system (충돌은 detect_spine 이 이미 걸러 단일)
    systems = set()
    for c in rows:
        v = (c.get("fields") or {}).get(spine)
        s = numbering_system_of(v)
        if s:
            systems.add(s)
    numbering_system = next(iter(systems)) if len(systems) == 1 else "arabic"

    # 번호 노드 수집 + 롤업/총계 분리
    # must-fix 6: 번호 없는 total(ALL/전체)은 트리 밖 canonical. 번호 있는 롤업(합계/소계
    #   /진척율)은 트리 구조엔 남기되 hierarchy_node 재발행 금지 → rollup_nos 로 표시.
    numbered: Dict[str, Dict[str, Any]] = {}
    rollup_nos: set = set()
    rollups_noanchor: List[Tuple[Dict[str, Any], str, Optional[str]]] = []
    for c in rows:
        raw = (c.get("fields") or {}).get(spine)
        num = norm_num(raw, in_spine=True)
        cls = _classify_rollup(c, spine, num)
        if cls == "total" and not num:
            rollups_noanchor.append((c, "total", None))
            continue  # 번호 없는 total 은 트리에 편입 안 함
        if num:
            numbered[num] = c  # 동일 system 내 last-wins (충돌 이미 배제됨)
            if cls in ("total", "rollup"):
                rollup_nos.add(num)

    if not numbered:
        return None
    allnums = set(numbered)

    # 시트 대표 source_file — 모든 실 table_row 는 동일 파일. 팬텀 노드가 실자식 좌표를
    # 못 찾는 다단 결번(≥2 연속 결번)에서도 source.file 이 비지 않도록 신뢰 fallback.
    # (must-fix 3 + must-fix 7: 모든 발행 청크 validate_chunk_schema()==[])
    sheet_src_file = ""
    for c in rows:
        sf = c.get("source_file")
        if sf:
            sheet_src_file = sf
            break

    # 팬텀 부모: 노드~최장관측prefix 사이 모든 결번 중간 prefix 합성 (must-fix 3)
    phantom_nos: set = set()
    for num in list(allnums):
        parts = num.split(".")
        # 최장 관측 부모 위치 찾기
        observed_parent = parent_of(num, allnums | phantom_nos)
        # 부모가 바로 위 prefix 가 아니면 그 사이 결번 prefix 를 팬텀으로
        for cut in range(1, len(parts)):
            pre = ".".join(parts[:cut])
            if pre not in allnums and pre not in phantom_nos:
                phantom_nos.add(pre)
    # 팬텀도 다시 상위 팬텀이 필요할 수 있음 (다단) — 위 루프가 모든 prefix 를 이미 커버
    full_nos = allnums | phantom_nos

    # children/parent/roots 재계산 (팬텀 포함)
    children: Dict[Optional[str], List[str]] = defaultdict(list)
    parent_map: Dict[str, Optional[str]] = {}
    roots: List[str] = []
    for num in sorted(full_nos, key=_num_key):
        p = parent_of(num, full_nos)
        parent_map[num] = p
        if p:
            children[p].append(num)
        else:
            roots.append(num)

    def ancestors(num: str) -> List[str]:
        chain, p = [], parent_map.get(num)
        while p:
            chain.append(p)
            p = parent_map.get(p)
        return list(reversed(chain))

    def label_for(num: str) -> str:
        c = numbered.get(num)
        return _label_of(c, spine) if c else ""

    def first_real_descendant(num: str) -> Optional[Dict[str, Any]]:
        """팬텀 노드의 좌표 접합용 — 서브트리 내 실제 numbered 행(가장 얕은 것) 반환."""
        stack = sorted(children.get(num, []), key=_num_key)
        while stack:
            k = stack.pop(0)
            real = numbered.get(k)
            if real is not None:
                return real
            stack = sorted(children.get(k, []), key=_num_key) + stack
        return None

    def breadcrumb_for(num: str) -> str:
        return " > ".join(f"{n}:{label_for(n)}" for n in ancestors(num))

    out: List[Dict[str, Any]] = []
    consumed: set = set()

    # 각 번호 노드 처리
    for num in sorted(full_nos, key=_num_key):
        kids = children.get(num, [])
        depth = num.count(".") + 1
        bc = breadcrumb_for(num)
        is_phantom = num in phantom_nos
        anchor = numbered.get(num)

        if num in rollup_nos and anchor is not None:
            # must-fix 6: 번호 내부노드라도 롤업 라벨이면 hierarchy_node 재발행 금지.
            # canonical 롤업 1회 + summarizes(직속 자식) 링크.
            cls = "total" if _classify_rollup(anchor, spine, num) == "total" else "rollup"
            summarizes = list(kids)
            out.append(_build_rollup(anchor, cls, num, spine, summarizes))
            consumed.add(id(anchor))
            continue

        if kids:
            # 내부노드 → hierarchy_node 요약청크 (must-fix 3,6,7)
            label = label_for(num)
            child_lines_all = [f"- {k} {label_for(k)}".rstrip() for k in kids]
            head_stub = f"[{sheet}] {num} {label}"

            def measure(grp: List[str]) -> int:
                return len(head_stub) + sum(len(x) + 1 for x in grp)

            subgroups = pack(child_lines_all, measure=measure, max_chars=max_chars)
            parted = assign_parts(subgroups, multi_only=False)
            # child_nos 를 part 별로 매핑
            idx = 0
            # confidence: 내부노드=min(자기+직속자식), 팬텀=자식 min
            confs = []
            if anchor is not None:
                cv = (anchor.get("quality") or {}).get("confidence")
                if isinstance(cv, (int, float)):
                    confs.append(cv)
            for k in kids:
                ck = numbered.get(k)
                if ck:
                    cv = (ck.get("quality") or {}).get("confidence")
                    if isinstance(cv, (int, float)):
                        confs.append(cv)
            conf = min(confs) if confs else 0.5
            review = False
            for src_c in ([anchor] if anchor else []) + [numbered.get(k) for k in kids]:
                if src_c and (src_c.get("quality") or {}).get("review_required"):
                    review = True

            for grp, pi, pt in parted:
                grp_nos = kids[idx:idx + len(grp)]
                idx += len(grp)
                last_child = None
                for k in reversed(grp_nos):
                    if numbered.get(k):
                        last_child = numbered[k]
                        break
                # 다단 결번(≥2 연속): 팬텀의 직속 자식도 팬텀이라 그룹에서 실행을 못
                # 찾을 수 있다 → 서브트리 최얕은 실자손을 좌표 접합에 사용(must-fix 3).
                if last_child is None and anchor is None:
                    last_child = first_real_descendant(num)
                carry = _carry_meta(anchor) if anchor else {}
                node_src_file = (anchor or {}).get("source_file") \
                    or (last_child or {}).get("source_file") or sheet_src_file
                node = _build_node(
                    anchor=anchor, node_no=num, label=label, breadcrumb=bc,
                    child_lines=grp, child_nos=grp_nos, depth=depth,
                    spine_kind=spine, numbering_system=numbering_system,
                    part_index=pi, part_total=pt, sheet=str(sheet),
                    src_file=node_src_file,
                    confidence=conf, review_required=review, phantom=is_phantom,
                    last_child=last_child, roots=list(roots), carry=carry)
                out.append(node)
            if anchor is not None:
                consumed.add(id(anchor))
        else:
            # leaf → table_row in-place metadata 태그 (must-fix 8)
            if anchor is not None:
                out.append(_tag_leaf(anchor, node_no=num, parent_no=parent_map.get(num),
                                     depth=depth, breadcrumb=bc, spine_kind=spine,
                                     numbering_system=numbering_system))
                consumed.add(id(anchor))
            # 팬텀 leaf 는 존재 불가(팬텀은 항상 자식을 가짐)

    # 번호 없는 total(ALL/전체) canonical 청크 — 전체 roots 를 summarizes
    for c, cls, num in rollups_noanchor:
        out.append(_build_rollup(c, cls, num, spine, list(roots)))
        consumed.add(id(c))

    # pass-through: 소비 안 된 table_row (비번호/공백/비고행) 원본 그대로 (must-fix 5)
    for c in rows:
        if id(c) not in consumed:
            out.append(c)

    return out


# ================================================================ 진입점
def merge_hierarchy_rows(chunks: List[Dict[str, Any]], *, max_chars: int = 1100,
                         min_numbered_ratio: float = 0.6) -> List[Dict[str, Any]]:
    """CGH 계층 병합. self-gating: 신호 미달 시 입력 그대로 반환.

    - table_row 만 처리, 그 외 chunk_type 은 pass-through.
    - 시트별 독립 게이팅. 어떤 입력청크도 드롭하지 않는다.
    """
    if max_chars <= 0:
        return list(chunks)

    # 시트별 table_row 수집 (원본 순서 유지 위해 인덱스 기록)
    rows_by_sheet: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for c in chunks:
        if c.get("chunk_type") == "table_row":
            rows_by_sheet[c.get("sheet")].append(c)

    # 시트별 처리 결과 (발화한 시트만)
    processed: Dict[Any, List[Dict[str, Any]]] = {}
    for sheet, rows in rows_by_sheet.items():
        res = _process_sheet(rows, chunks, sheet, max_chars=max_chars,
                             min_ratio=min_numbered_ratio)
        if res is not None:
            processed[sheet] = res

    if not processed:
        return list(chunks)  # 어떤 시트도 발화 안 함 → 원본

    # 재조립: 원본 순서 유지. 각 시트의 첫 table_row 위치에 처리결과 삽입,
    # 나머지 table_row(및 소비된 것) 은 스킵. 발화 안 한 시트의 청크는 그대로.
    out: List[Dict[str, Any]] = []
    inserted: set = set()
    active_row_ids: Dict[Any, set] = {
        sheet: {id(c) for c in rows_by_sheet[sheet]} for sheet in processed
    }
    for c in chunks:
        sheet = c.get("sheet")
        if sheet in processed:
            if c.get("chunk_type") == "table_row" and id(c) in active_row_ids[sheet]:
                if sheet not in inserted:
                    out.extend(processed[sheet])
                    inserted.add(sheet)
                # 이 시트의 table_row 는 processed 안에 이미 반영됨 → 스킵
                continue
        out.append(c)
    return out
