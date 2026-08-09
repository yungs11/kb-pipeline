#!/usr/bin/env python
"""global 검색의 **리포트 선정**이 질문을 반영하는지 확인한다 (배포 전 게이트).

무엇을 검사하나
---------------
`kb_pipeline/community.py:_rank_reports` 는 질문 토큰과 리포트 본문의 **부분문자열
겹침 개수**로 리포트를 고르고, 동점이면 `rank`(커뮤니티 크기 기반)로 가른다.

    q_terms = {t for t in re.split(r"\\W+", question.lower()) if len(t) > 1}
    overlap = sum(1 for term in q_terms if term in hay)

선정 단계는 **순수 파이썬이다 — LLM 을 부르지 않는다.** 그래서 Postgres 만 있으면
돌고, LLM 게이트웨이가 죽어 있어도 검사할 수 있다(답변 생성만 LLM 이 필요하고, 그건
이 게이트의 대상이 아니다).

질문을 두 부류로 나눠 본다 — 섞으면 정상을 결함으로, 결함을 정상으로 읽는다
---------------------------------------------------------------------------
**넓은 질문**("전체적으로 무슨 내용인가") 은 리포트 본문의 구체어를 쓰지 않으므로
**겹침 0 이 정상이다.** 그때 `rank` 순 = 큰 커뮤니티 순으로 고르는 것은 "전체 요약"
용도에 오히려 맞다. 여기서 겹침 0 을 결함으로 세면 안 된다(실측 확인: 2026-08-09).

**특정 주제 질문**은 다르다. 리포트에 실제로 있는 주제어를 담고 있으니 겹침이 잡혀야
하고, 관련 리포트가 상위에 와야 한다. 여기서 겹침 0 이면 조사·어미 때문에 토큰이
전부 빗나간 것이고, 그러면 **질문과 무관한 큰 커뮤니티 5개**로 답을 만든다. 답변은
그럴듯하게 나오므로 **답변만 봐서는 드러나지 않는다.**

실측된 실제 실패 모드(2026-08-09):
  * `"이사할 때 필요한 절차가 있나?"` → 토큰 `{이사할, 때, 필요한, 절차가}`.
    리포트가 "거주지 이전 및 주소 변경 절차" 라도 `절차가` 는 `절차.` 의 부분문자열이
    아니라 **겹침 0** → 무관한 리포트 선정.
  * `"주소 변경 절차"` 는 정답을 고르지만 `"주소변경 절차"`(붙여쓰기)는 오답을 고른다.
    **띄어쓰기 하나로 뒤집힌다.**
같은 뿌리의 실패를 전에 겪었다: "이사" 질문이 "거주지 이전시 1일" 문서를 못 찾은 건.

판정
----
  * 특정 주제 질문에서 **겹침 0 이 하나라도** 나오면 → 보류하고 사람이 판단한다.
  * 특정 주제 질문의 상위 리포트가 무관해 보이면 → 보류.
  * 넓은 질문은 겹침 0 이어도 통과. 다만 고른 집합이 **매번 완전히 동일**한지 보고,
    그게 의도(상위 rank 고정)와 맞는지 확인한다.

사용법
------
    # 1) 리포트가 있는 워크스페이스 찾기
    python scripts/check_global_rank.py --dsn "$KBP_PG_DSN" --list

    # 2) 게이트 실행 — 특정 주제 질문은 **이 KB 의 실제 주제어**로 줘야 의미가 있다
    python scripts/check_global_rank.py --dsn "$KBP_PG_DSN" \\
        --workspace <eq-ws-uuid> --specific specific.txt

`--specific` 를 안 주면 리포트 제목에서 주제어를 뽑아 질문을 **자동 생성**한다
(제목 단어가 본문에 있는 것은 당연하므로 관대한 검사다 — 그래도 여기서 0 이 나오면
확실한 결함이다).
"""
from __future__ import annotations

import argparse
import os
import re
import sys

# 리포 루트를 sys.path 에 넣는다 — pytest 는 rootdir 을 자동으로 넣지만 스크립트
# 직접 실행은 그렇지 않다(워크트리에서 `python scripts/...` 하면 ImportError).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 검사 대상은 **실제 출하 코드**다 — 순위 로직을 여기 다시 구현하면 사본을 검사하는
# 셈이 되어 아무것도 보장하지 못한다.
from kb_pipeline.community import _rank_reports  # noqa: E402

#: 넓은 질문 — global 검색의 본래 용도. 겹침 0 이 정상이다.
BROAD_QUESTIONS = [
    "이 지식베이스는 전체적으로 무슨 내용인가?",
    "가장 자주 나오는 주제 세 가지는 무엇인가?",
    "전체 내용을 요약해줘",
    "핵심 쟁점은 무엇인가?",
    "문서들 사이의 공통 주제는?",
]


def _terms(q: str) -> set[str]:
    return {t for t in re.split(r"\W+", q.lower()) if len(t) > 1}


def overlap_of(rep: dict, question: str) -> int:
    """`_rank_reports` 의 점수를 **표시용**으로 재현한다.

    순위 자체는 `_rank_reports`(출하 코드)가 정한다 — 이 값은 왜 그렇게 골랐는지
    사람에게 보여주기 위한 것이다.
    """
    hay = (rep.get("title", "") + " " + rep.get("summary", "")).lower()
    for f in rep.get("findings", []):
        if isinstance(f, dict):
            hay += " " + f.get("summary", "") + " " + f.get("explanation", "")
    return sum(1 for t in _terms(question) if t in hay)


def fetch_reports(dsn: str, workspace_id: str, level: int) -> list[dict]:
    import psycopg

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT community_id, title, summary, findings, rank, entity_ids"
            "  FROM public.community_reports"
            " WHERE workspace_id = %s AND level = %s",
            (workspace_id, level),
        )
        return [
            {
                "community_id": cid,
                "title": title,
                "summary": summary or "",
                "findings": findings or [],
                "rank": rank if rank is not None else 0.0,
                "entity_ids": ents or [],
            }
            for cid, title, summary, findings, rank, ents in cur.fetchall()
        ]


def derive_specific(reports: list[dict], n: int = 8) -> list[tuple[str, int]]:
    """리포트 제목에서 주제어를 뽑아 특정 주제 질문을 만든다.

    제목 단어가 그 리포트 본문에 있는 것은 당연하므로 **관대한 검사**다. 그래도
    여기서 겹침 0 이나 오선정이 나오면 확실한 결함이다.
    :returns: `(질문, 기대 community_id)` 목록.
    """
    out = []
    for rep in sorted(reports, key=lambda r: -float(r.get("rank") or 0))[:n]:
        title = (rep.get("title") or "").strip()
        words = [w for w in re.split(r"\W+", title) if len(w) > 1][:3]
        if len(words) < 2:
            continue
        out.append((" ".join(words) + " 에 대해 알려줘", rep["community_id"]))
    return out


def show(label: str, q: str, selected: list[dict], flag: str = "   ") -> list[int]:
    print(f"{flag}[{label}] {q}")
    scores = []
    for r in selected:
        sc = overlap_of(r, q)
        scores.append(sc)
        print(f"       겹침 {sc:>2}  rank {float(r.get('rank') or 0):>5.2f}"
              f"  #{r['community_id']}  {(r.get('title') or '')[:64]}")
    return scores


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get("KBP_PG_DSN"))
    ap.add_argument("--workspace", help="edgequake 워크스페이스 UUID(kb id 아님)")
    ap.add_argument("--level", type=int, default=0)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--specific", help="특정 주제 질문 파일(한 줄에 하나)")
    ap.add_argument("--list", action="store_true", help="리포트가 있는 워크스페이스 나열")
    a = ap.parse_args()

    if not a.dsn:
        print("DSN 이 없다 — --dsn 또는 KBP_PG_DSN", file=sys.stderr)
        return 2

    if a.list:
        import psycopg

        with psycopg.connect(a.dsn) as conn:
            rows = conn.execute(
                "SELECT workspace_id, level, count(*), max(created_at), min(created_at)"
                "  FROM public.community_reports GROUP BY 1,2 ORDER BY 3 DESC"
            ).fetchall()
        if not rows:
            print("리포트가 하나도 없다 — 야간 배치가 안 돌았거나 그래프가 비었다.")
            return 1
        for ws, lvl, n, newest, oldest in rows:
            print(f"  {ws}  level={lvl}  {n}건  최근 {newest}  최고령 {oldest}")
        return 0

    if not a.workspace:
        print("--workspace 가 필요하다(--list 로 확인)", file=sys.stderr)
        return 2

    reports = fetch_reports(a.dsn, a.workspace, a.level)
    if not reports:
        print(f"level={a.level} 리포트가 없다 — 검사할 수 없다.")
        return 1
    print(f"리포트 {len(reports)}건 / top_k={a.top_k}\n")

    # ── 넓은 질문 — 겹침 0 은 정상. 선정 안정성만 본다 ──────────────────────
    print("═" * 72)
    print("넓은 질문 (global 검색의 본래 용도 — 겹침 0 이 정상)")
    print("═" * 72)
    broad_sets = set()
    for q in BROAD_QUESTIONS:
        sel = _rank_reports(reports, q, a.top_k)
        show("넓음", q, sel)
        broad_sets.add(tuple(r["community_id"] for r in sel))
        print()

    # ── 특정 주제 질문 — 겹침 0 이면 결함 ───────────────────────────────────
    if a.specific:
        with open(a.specific) as fh:
            specific = [(ln.strip(), None) for ln in fh if ln.strip()]
        source = f"파일 {a.specific}"
    else:
        specific = derive_specific(reports)
        source = "리포트 제목에서 자동 생성(관대한 검사)"

    print("═" * 72)
    print(f"특정 주제 질문 — {source}")
    print("  겹침 0 이면 조사·어미로 토큰이 전부 빗나갔다는 뜻 = 무관한 리포트로 답한다")
    print("═" * 72)
    blind, misplaced, tied = [], [], []
    spec_sets: dict[tuple[int, ...], list[str]] = {}
    for q, expect in specific:
        sel = _rank_reports(reports, q, a.top_k)
        scores = show("특정", q, sel, flag="   ")
        key = tuple(r["community_id"] for r in sel)
        spec_sets.setdefault(key, []).append(q)

        if not scores or max(scores) == 0:
            blind.append(q)
            print("       ↑ ⚠️ 겹침 0 — 질문을 보지 않고 rank 순서로 골랐다")
        elif len(set(scores)) == 1:
            # ★ 겹침 0 만 세면 이걸 놓친다 — 겹침이 전부 같으면 **정렬은 rank 가 한다.**
            #   실측 예: "장모님 팔순…" / "처제 결혼…" / "이사하는데 휴가 나오나?" 가
            #   모두 겹침 1(그 1 은 전부 "휴가" 한 단어)로 **같은 리포트 3개**를 골랐다.
            #   변별력 없는 흔한 단어 하나만 걸린 것이라 겹침 0 과 실질이 같다.
            hits = sorted(t for t in _terms(q)
                          if all(t in ((r.get("title", "") + " " + (r.get("summary") or "")).lower())
                                 for r in sel))
            tied.append(q)
            print(f"       ↑ ⚠️ 겹침 동점({scores[0]}) — 순서를 rank 가 정했다."
                  f" 공통 히트: {hits or '없음'}")
        if expect is not None and sel and sel[0]["community_id"] != expect:
            misplaced.append((q, expect, sel[0]["community_id"]))
            print(f"       ↑ ⚠️ 기대 #{expect} 가 1위가 아니다(1위 #{sel[0]['community_id']})")
        print()

    dup = {k: v for k, v in spec_sets.items() if len(v) > 1}
    if dup:
        print("─" * 72)
        print("⚠️ 서로 다른 질문이 **같은 리포트 집합**을 골랐다 — 선정이 질문을 구분하지 못한다")
        for k, qs in dup.items():
            print(f"   {list(k)} ← {len(qs)}개 질문")
            for q in qs:
                print(f"       · {q}")
        print()

    # ── 판정 ────────────────────────────────────────────────────────────────
    print("─" * 72)
    print(f"넓은 질문 선정 집합 종류: {len(broad_sets)}/{len(BROAD_QUESTIONS)}"
          "  (1 = 항상 같은 상위 rank — 전체 요약 용도로는 의도된 동작일 수 있다)")
    print(f"특정 주제 질문 중 겹침 0: {len(blind)}/{len(specific)}")
    print(f"           겹침 동점(rank 가 정렬): {len(tied)}/{len(specific)}"
          "  — 겹침 0 과 실질이 같다")
    print(f"        서로 다른 선정 집합: {len(spec_sets)}/{len(specific)}")
    if misplaced:
        print(f"기대 리포트가 1위가 아닌 경우: {len(misplaced)}건")

    if blind or tied or misplaced:
        print("\n❌ 배포 보류 — 특정 주제 질문에서 선정이 질문을 반영하지 못한다.")
        print("   답변은 그럴듯하게 나오므로 이 신호는 답변만 봐서는 드러나지 않는다.")
        print("   완화책: 사용자에게 global 은 '넓은 질문' 용도임을 UI 에 명시하고,")
        print("   특정 주제 질문은 local 로 유도한다(현재 토글 설명이 그 역할을 한다).")
        return 1

    print("\n✅ 선정 단계가 질문을 반영한다.")
    print("   남은 확인: 위 목록의 리포트 제목이 질문과 실제로 맞는지 눈으로 본다"
          " — 그 판단은 자동화 대상이 아니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
