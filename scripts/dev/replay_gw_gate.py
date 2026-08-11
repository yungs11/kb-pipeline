#!/usr/bin/env python3
"""GW 게이트 오프라인 리플레이 — 게이트웨이 없이 V3/V4 를 재현한다.

**왜 오프라인인가**: 게이트웨이 장애(530)로 스캔레인 작업이 중단된 전례가 있고 폴백 경로로
우회 측정하는 것은 금지돼 있다. 하네스 재실행에 의존하면 검증이 교착된다.

**역할 분리**: 이 스크립트는 **개발자 로컬 1회 검증(V3/V4)** 이다.
CI 회귀 감시는 `parse_service/tests/fixtures/` 의 PII-free 픽스처가 담당한다 —
코퍼스는 PII 를 포함하므로 리포에 커밋하지 않고, 없으면 skip 종료한다.

**측정 단계 차이**: 프로덕션 게이트는 `_supplement_diagram_pages` **뒤**에 있는데 이 리플레이는
게이트웨이 원출력을 태운다 = **quarantine 발화의 상한값**(방향은 보수적 — 프로덕션에서는
diagram 페이지가 VL 서술로 채워져 발화가 줄기만 한다).

사용:
    scripts/dev/replay_gw_gate.py [--corpus DIR]
    KBP_GW_REPLAY_CORPUS=... scripts/dev/replay_gw_gate.py
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from kb_pipeline.blockify import hybrid_to_blocks                      # noqa: E402
from parse_service.parsers.degen_filter import visible_chars           # noqa: E402
from parse_service.parsers.pdf import page_verdict as pv               # noqa: E402
from parse_service.parsers.pdf.page_verdict import (                   # noqa: E402
    PageState, apply_gw_page_gate,
)

DEFAULT_CORPUS = os.environ.get(
    "KBP_GW_REPLAY_CORPUS",
    os.path.expanduser("~/workspace/9.kbp-parser-compare"),
)

#: 코퍼스는 2026-08-11 에 주제별로 재구성됐다(`gw_raw/` → `results/raw/gw/` 등).
#: 옛 평면 구조도 계속 받아 준다 — 백업/아카이브 사본이 그 형태일 수 있다.
_LAYOUTS = (
    {"raw": "results/raw/gw", "runs": "results/metrics/qual_gw_runs.tsv",
     "labels": "docs/analysis/GW_LABELS_EVIDENCE.md"},          # 신 구조
    {"raw": "gw_raw", "runs": "qual_gw_runs.tsv",
     "labels": "GW_LABELS_EVIDENCE.md"},                        # 구 평면 구조
)


def resolve_paths(corpus: str):
    """(raw_dir, runs_tsv, labels_md) — 신/구 구조를 모두 지원. 없으면 None."""
    for lay in _LAYOUTS:
        paths = tuple(os.path.join(corpus, lay[k]) for k in ("raw", "runs", "labels"))
        if all(os.path.exists(x) for x in paths):
            return paths
    return None

#: 라벨 문서에서 **절 경계로 인정할 헤더는 이 셋뿐**이다. 본문 오염으로 생긴 가짜 `## `
#: 헤더가 실재하므로(:107, :155) 순진하게 `^## ` 로 쪼개면 분류가 어긋난다.
_SECTIONS = (
    "## UNUSABLE 11건 — 근거 원문",
    "## 경계 사례 — USABLE 로 판정했으나 이견 가능",
    "## USABLE 41건 (경계 제외) — 목록",
)
_HDR_RE = re.compile(r"^### \[\d+\]\s+(\S+)", re.M)
_BUL_RE = re.compile(r"^- \*\*\[\d+\]\*\*\s+`([^`]+)`", re.M)


def load_labels(path: str) -> dict[str, str]:
    """slug → "UNUSABLE" | "USABLE".

    라벨은 **두 포맷**으로 들어 있다 — 헤더형 `### [n] <slug>` 19건(UNUSABLE 11 + 경계 8)과
    불릿형 ``- **[n]** `<slug>` `` 41건(USABLE). 헤더형만 파싱하면 라벨이 19페이지에만 붙어
    분모 49 를 만들 수 없는데 스크립트는 에러 없이 "observed FP 0/19" 로 통과한다.
    """
    text = open(path, encoding="utf-8").read()
    lines = text.splitlines()
    bounds = [i for i, ln in enumerate(lines) if ln.strip() in _SECTIONS]
    if len(bounds) != 3:
        raise SystemExit(f"라벨 문서 절 헤더 3개를 못 찾았다: {len(bounds)}개 — {path}")
    unusable_zone = (bounds[0], bounds[1])

    labels: dict[str, str] = {}
    for m in _HDR_RE.finditer(text):
        line_no = text[:m.start()].count("\n")
        slug = m.group(1)
        labels[slug] = ("UNUSABLE" if unusable_zone[0] < line_no < unusable_zone[1]
                        else "USABLE")   # 경계 사례는 USABLE 로 판정된 것들이다
    for m in _BUL_RE.finditer(text):
        labels.setdefault(m.group(1), "USABLE")

    n_un = sum(1 for v in labels.values() if v == "UNUSABLE")
    n_us = len(labels) - n_un
    if not (len(labels) == 60 and n_us == 49 and n_un == 11):
        raise SystemExit(f"라벨 파싱 실패 — 60/49/11 이어야 하는데 "
                         f"{len(labels)}/{n_us}/{n_un} 이다 (파싱 규칙을 고쳐라)")
    return labels


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=DEFAULT_CORPUS)
    args = ap.parse_args()
    resolved = resolve_paths(args.corpus)
    if resolved is None:
        print(f"SKIP — 코퍼스 없음: {args.corpus}\n"
              f"      기대 구조(둘 중 하나):\n"
              f"        results/raw/gw + results/metrics/qual_gw_runs.tsv + docs/analysis/GW_LABELS_EVIDENCE.md\n"
              f"        gw_raw + qual_gw_runs.tsv + GW_LABELS_EVIDENCE.md\n"
              f"      (PII 를 포함해 리포에 커밋하지 않는다. --corpus 로 경로를 주거나\n"
              f"       KBP_GW_REPLAY_CORPUS 를 설정하라. CI 회귀는 tests/fixtures 가 담당한다.)")
        return 0
    raw_dir, runs_tsv, labels_md = resolved
    print(f"코퍼스: {args.corpus}\n  raw={os.path.relpath(raw_dir, args.corpus)}")

    labels = load_labels(labels_md)
    rows = list(csv.DictReader(open(runs_tsv, encoding="utf-8"), delimiter="\t"))
    ink_by_slug = {r["slug"]: float(r["ink"]) for r in rows if r.get("ink")}

    # ink 는 새로 렌더하지 않고 하네스 실측값을 주입한다(정의: dpi100 GRAY dark-fraction).
    slug_of_page: dict[int, str] = {}
    pv_page_ink = pv.page_ink
    pv.page_ink = lambda fb, p: ink_by_slug.get(slug_of_page.get(p, ""), None)  # type: ignore

    registry_kept = {}
    results = []
    try:
        for i, r in enumerate(rows, 1):
            slug = r["slug"]
            md_path = os.path.join(raw_dir, f"{slug}.md")
            if not os.path.exists(md_path):
                continue
            blocks = hybrid_to_blocks(open(md_path, encoding="utf-8").read(), page_idx=i)
            page = {"page_number": i, "blocks": blocks, "status": "ok"}
            slug_of_page[i] = slug
            before = sum(visible_chars((b.get("table_body") if b.get("type") == "table"
                                        else b.get("text")) or "") for b in blocks)
            (v,) = apply_gw_page_gate([page], b"", diagram_pages=())
            after = sum(visible_chars((b.get("table_body") if b.get("type") == "table"
                                       else b.get("text")) or "") for b in page["blocks"])
            results.append((slug, labels.get(slug, "?"), v, before, after, r["doc"], r["page"]))
            if "죽림현대" in r["doc"] or "장현지구" in r["doc"]:
                registry_kept[r["doc"][:22]] = (before, after)
    finally:
        pv.page_ink = pv_page_ink  # type: ignore

    q = [x for x in results if x[2].state is PageState.QUARANTINED_FAILURE]
    tp = [x for x in q if x[1] == "UNUSABLE"]
    fp = [x for x in q if x[1] == "USABLE"]
    skipped = [x for x in results if x[2].state is PageState.EMPTY_SKIPPED]
    n_un = sum(1 for x in results if x[1] == "UNUSABLE")
    n_us = sum(1 for x in results if x[1] == "USABLE")

    print(f"\n■ V3 — degen 안전화: 등기부 표 보존")
    for doc, (b, a) in sorted(registry_kept.items()):
        ok = "✓ 보존" if a >= b else f"✗ {b - a}자 손실"
        print(f"   {doc:<24} {b:>5} → {a:<5}  {ok}")

    print(f"\n■ V4 — 게이트 판정 (총 {len(results)}p / 라벨 USABLE {n_us} · UNUSABLE {n_un})")
    print(f"   quarantine {len(q)}   recall {len(tp)}/{n_un}"
          f"   **observed FP {len(fp)}/{n_us}**")
    for slug, lab, v, b, a, doc, pg in q:
        print(f"     [{lab:<8}] {b:>5}→{a:<5} {v.reason[:46]:<48} {doc[:30]} p{pg}")
    print(f"   EMPTY_SKIPPED {len(skipped)} (quarantine 아님, blocks 보존):")
    for slug, lab, v, b, a, doc, pg in skipped:
        print(f"     [{lab:<8}] {b:>5}→{a:<5} {v.reason[:46]:<48} {doc[:30]} p{pg}")

    print("\n   ⚠️ 이 수치는 리플레이 산출물에 대한 등호다(고정 입력 → 결정적).")
    print("      프로덕션은 게이트가 _supplement_diagram_pages 뒤라 발화가 **이하로만** 움직인다.")
    print("      CJK 문서 가드는 이 코퍼스가 60p/59문서(1페이지씩)라 미적용 = 미검증.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
