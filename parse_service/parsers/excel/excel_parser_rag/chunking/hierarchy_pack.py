"""계층 병합 공용 레이어: greedy cap 분할 + part 번호 (spec 2026-06-26 WBS).

도메인(전결/WBS)은 키 추출·런 구성·청크 빌드를 각자 하고, 까다로운 cap 패킹만 여기서 공유한다.
"""
from __future__ import annotations

from typing import Callable, List, Tuple


def pack(items: List, *, measure: Callable[[List], int], max_chars: int) -> List[List]:
    """items 를 입력 순서대로, measure(subgroup) <= max_chars 인 subgroup 들로 greedy 분할.
    단일 item 이 혼자 한계를 넘어도 분할하지 않는다(그 자체로 한 subgroup)."""
    subgroups: List[List] = []
    cur: List = []
    for it in items:
        if cur and measure(cur + [it]) <= max_chars:
            cur.append(it)
        else:
            if cur:
                subgroups.append(cur)
            cur = [it]
    if cur:
        subgroups.append(cur)
    return subgroups


def assign_parts(subgroups: List[List], *, multi_only: bool = True) -> List[Tuple]:
    """[(subgroup, part_index, part_total)] 반환.
    multi_only=True: len>=2 subgroup 만 part 로 카운트(단일은 part_index=0). delegation 용.
    multi_only=False: 모든 subgroup 을 1..N 으로 카운트. WBS 용(부모+자식이라 항상 병합)."""
    if multi_only:
        total = sum(1 for g in subgroups if len(g) >= 2)
    else:
        total = len(subgroups)
    out: List[Tuple] = []
    part = 0
    for g in subgroups:
        if multi_only and len(g) < 2:
            out.append((g, 0, total))
        else:
            part += 1
            out.append((g, part, total))
    return out
