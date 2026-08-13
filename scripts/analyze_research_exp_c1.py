#!/usr/bin/env python3
"""Human-grounded policy and feature analysis for EXP-C1 run-01."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import statistics


REPO = Path(__file__).resolve().parents[1]
RUN = REPO / "research/EXP-C1-20260811/run-01"


def main() -> None:
    features = {r["sample_id"]: r for r in csv.DictReader(
        (RUN / "features.tsv").open(encoding="utf-8"), delimiter="\t")}
    reviews = list(csv.DictReader((RUN / "human_review.tsv").open(encoding="utf-8"), delimiter="\t"))

    def n(field: str, value: str, engine: str = "") -> int:
        key = f"{engine}_{field}" if engine else field
        return sum(r[key] == value for r in reviews)

    beneficial = {r["sample_id"] for r in reviews if r["vl_beneficial"] == "true"}
    v1_quarantine = {sid for sid, f in features.items() if f["v1_verdict"] == "quarantine"}
    no_table = {sid for sid, f in features.items() if int(f["gw_tables"]) == 0}
    chars_lt_400 = {sid for sid, f in features.items() if int(f["gw_chars"]) < 400}
    chars_lt_500 = {sid for sid, f in features.items() if int(f["gw_chars"]) < 500}

    policy = {
        "OCR-only": {
            "retrieval_pass": n("retrieval", "RETRIEVAL_PASS", "gw"),
            "retrieval_borderline": n("retrieval", "RETRIEVAL_BORDERLINE", "gw"),
            "critical_error": n("fidelity", "CRITICAL_ERROR", "gw"),
            "confirmed_hallucination": n("safety", "HALLUCINATION_CONFIRMED", "gw"),
            "quarantine": 0, "vl_calls": 0,
        },
        "VL-only": {
            "retrieval_pass": n("retrieval", "RETRIEVAL_PASS", "vl"),
            "retrieval_borderline": n("retrieval", "RETRIEVAL_BORDERLINE", "vl"),
            "critical_error": n("fidelity", "CRITICAL_ERROR", "vl"),
            "confirmed_hallucination": n("safety", "HALLUCINATION_CONFIRMED", "vl"),
            "empty_or_truncated": sum(r["vl_coverage"] in {"EMPTY", "TRUNCATED"} for r in reviews),
            "quarantine": 0, "vl_calls": 16,
        },
        "v1": {
            "retrieval_pass": sum(r["gw_retrieval"] == "RETRIEVAL_PASS" and r["sample_id"] not in v1_quarantine for r in reviews),
            "retrieval_borderline": sum(r["gw_retrieval"] == "RETRIEVAL_BORDERLINE" and r["sample_id"] not in v1_quarantine for r in reviews),
            "critical_error_in_final": sum(r["gw_fidelity"] == "CRITICAL_ERROR" and r["sample_id"] not in v1_quarantine for r in reviews),
            "confirmed_hallucination": 0, "quarantine": len(v1_quarantine), "vl_calls": 0,
        },
        "oracle_selective_VL": {
            "retrieval_pass": sum(
                (r["vl_retrieval"] if r["sample_id"] in beneficial else r["gw_retrieval"]) == "RETRIEVAL_PASS"
                and r["sample_id"] not in v1_quarantine for r in reviews),
            "retrieval_borderline": sum(
                (r["vl_retrieval"] if r["sample_id"] in beneficial else r["gw_retrieval"]) == "RETRIEVAL_BORDERLINE"
                and r["sample_id"] not in v1_quarantine for r in reviews),
            "critical_error_in_final": sum(
                (r["vl_fidelity"] if r["sample_id"] in beneficial else r["gw_fidelity"]) == "CRITICAL_ERROR"
                and r["sample_id"] not in v1_quarantine for r in reviews),
            "confirmed_hallucination": 0, "quarantine": len(v1_quarantine), "vl_calls": len(beneficial),
        },
    }

    def candidate(selected: set[str]) -> dict:
        return {
            "selected": len(selected), "beneficial_selected": len(selected & beneficial),
            "beneficial_total": len(beneficial),
            "precision_on_DEV": f"{len(selected & beneficial)}/{len(selected)}" if selected else "0/0",
            "recall_on_DEV": f"{len(selected & beneficial)}/{len(beneficial)}",
            "selected_ids": sorted(selected),
        }

    gw_seconds = [float(f["gw_seconds"]) for f in features.values()]
    vl_seconds = [float(f["vl_seconds"]) for f in features.values()]
    metrics = {
        "N": 16, "split": "DEV_convenience_nonrepresentative_legacy_strata_invalid",
        "beneficial": f"{len(beneficial)}/16", "beneficial_ids": sorted(beneficial),
        "v1_quarantine": f"{len(v1_quarantine)}/16", "v1_quarantine_ids": sorted(v1_quarantine),
        "v1_quarantine_vl_rescue": f"{len(v1_quarantine & beneficial)}/{len(v1_quarantine)}",
        "vl_empty": f"{n('coverage', 'EMPTY', 'vl')}/16",
        "vl_truncated": f"{n('coverage', 'TRUNCATED', 'vl')}/16",
        "vl_confirmed_hallucination": f"{n('safety', 'HALLUCINATION_CONFIRMED', 'vl')}/16",
        "gw_critical_error": f"{n('fidelity', 'CRITICAL_ERROR', 'gw')}/16",
        "vl_critical_error": f"{n('fidelity', 'CRITICAL_ERROR', 'vl')}/16",
        "policies": policy,
        "candidate_features": {
            "v1_hard_gate_as_vl_trigger": candidate(v1_quarantine),
            "gw_no_table": candidate(no_table),
            "gw_chars_lt_400_POST_HOC": candidate(chars_lt_400),
            "gw_chars_lt_500_POST_HOC": candidate(chars_lt_500),
        },
        "latency_seconds": {
            "gw_sum": round(sum(gw_seconds), 1), "gw_p50": round(statistics.median(gw_seconds), 1),
            "gw_max": round(max(gw_seconds), 1),
            "vl_sum": round(sum(vl_seconds), 1), "vl_p50": round(statistics.median(vl_seconds), 1),
            "vl_max": round(max(vl_seconds), 1),
            "execution": "GW/VL concurrent per page; three page batches; submit_gap=0",
        },
    }
    (RUN / "policy_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    disagree = [r for r in reviews if r["winner"] != "TIE"]
    with (RUN / "disagreements.tsv").open("w", encoding="utf-8", newline="") as handle:
        cols = list(disagree[0])
        wr = csv.DictWriter(handle, fieldnames=cols, delimiter="\t"); wr.writeheader(); wr.writerows(disagree)

    examples = RUN / "examples"; examples.mkdir(exist_ok=True)
    (examples / "INDEX.md").write_text("""# Representative original-grounded cases

- TP / silent-term rescue: [I19 original](../page_images/I19_01_양주시_옥정동_관토신_책준_서울중앙_분양대금반환등_장성근_소장_pdf_p35.jpg)
- TP / party-coverage rescue: [I26 original](../page_images/I26_01_창원시_팔용동_관토신_서울중앙_손해배상_등_이경연외2_소장_pdf_p270.jpg)
- TP / sparse-context rescue: [I35 original](../page_images/I35_01_남양주시_금곡리_대리사무_서울중앙_추심금_김성수_소장_pdf_p18.jpg)
- hard gate / both fail: [I24 original](../page_images/I24_01_경산삼남지역주택조합_대리사무_대구지법_손해배상_기_강성대외95_소장_pdf_p419.jpg)
- harmful VL truncation: [I49 original](../page_images/I49_05_안산시_성곡동_관토신_책준_안산지원_매매대금반환_박신숙_판결문_pdf_p4.jpg)
- confirmed VL clause-number corruption: [I10 original](../page_images/I10_01_세종시_대평동_관토신_서울중앙_분양대금등반환_소미영_소장_pdf_p34.jpg)
""", encoding="utf-8")


if __name__ == "__main__":
    main()
