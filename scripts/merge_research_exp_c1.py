#!/usr/bin/env python3
"""Merge successful EXP-C1 batches into one auditable run and review packet."""
from __future__ import annotations

import csv
import hashlib
import html
import json
from pathlib import Path
import shutil


REPO = Path(__file__).resolve().parents[1]
EXP = REPO / "research/EXP-C1-20260811"
OUT = EXP / "run-01"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    for name in ("page_images", "gw_raw", "vl_raw", "normalized"):
        (OUT / name).mkdir(parents=True, exist_ok=True)

    merged = []
    sample_by_index = {}
    manifests = []
    for batch in sorted(EXP.glob("batch-*")):
        manifests.append(json.loads((batch / "manifest.json").read_text(encoding="utf-8")))
        samples = list(csv.DictReader((batch / "sample.tsv").open(encoding="utf-8"), delimiter="\t"))
        sample_by_index.update({int(r["source_index"]): r for r in samples})
        for row in csv.DictReader((batch / "features.tsv").open(encoding="utf-8"), delimiter="\t"):
            idx = int(row["source_index"])
            old_image = batch / row["image_file"]
            base = old_image.stem.split("_", 1)[1]
            new_stem = f"I{idx:02d}_{base}"
            mapping = {
                "image_file": (old_image, OUT / "page_images" / f"{new_stem}.jpg"),
                "gw_file": (next((batch / "gw_raw").glob(f"{old_image.stem}.md")),
                            OUT / "gw_raw" / f"{new_stem}.md"),
                "vl_raw_file": (next((batch / "vl_raw").glob(f"{old_image.stem}.json")),
                                OUT / "vl_raw" / f"{new_stem}.json"),
                "vl_file": (next((batch / "normalized").glob(f"{old_image.stem}_vl.md")),
                            OUT / "normalized" / f"{new_stem}_vl.md"),
            }
            for key, (source, target) in mapping.items():
                shutil.copy2(source, target)
                row[key] = str(target.relative_to(OUT))
            # Markdown preview servers can return 404 for long Unicode URLs.
            # Keep the canonical image filename in features.tsv and add a short
            # ASCII alias specifically for REVIEW.md rendering.
            shutil.copy2(OUT / row["image_file"], OUT / "page_images" / f"I{idx:02d}.jpg")
            if sha(OUT / row["image_file"]) != row["image_sha256"]:
                raise RuntimeError(f"image hash mismatch: index {idx}")
            row["sample_id"] = f"I{idx:02d}"
            merged.append(row)
    merged.sort(key=lambda r: int(r["source_index"]))
    if len(merged) != 16 or len({r["source_index"] for r in merged}) != 16:
        raise RuntimeError("expected 16 unique completed rows")

    with (OUT / "features.tsv").open("w", encoding="utf-8", newline="") as handle:
        wr = csv.DictWriter(handle, fieldnames=list(merged[0]), delimiter="\t")
        wr.writeheader(); wr.writerows(merged)
    sample_cols = list(next(iter(sample_by_index.values())))
    with (OUT / "sample.tsv").open("w", encoding="utf-8", newline="") as handle:
        wr = csv.DictWriter(handle, fieldnames=sample_cols, delimiter="\t")
        wr.writeheader()
        for idx in sorted(sample_by_index):
            row = dict(sample_by_index[idx]); row["sample_id"] = f"I{idx:02d}"; wr.writerow(row)

    manifest = dict(manifests[0])
    manifest.update({
        "run_id": "run-01", "batch_count": 3,
        "sample_indices": [int(r["source_index"]) for r in merged],
        "execution_note": "GW and VL called concurrently per page; three page batches; per-engine max concurrency 3",
        "preflight_note": "Earlier sandbox-network attempt is outside run-01 and excluded",
    })
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    auto = {
        "N": 16,
        "gw_engine_errors": sum(bool(r["gw_error"]) for r in merged),
        "vl_engine_errors_or_empty": sum(bool(r["vl_error"]) for r in merged),
        "vl_empty": sum(int(r["vl_chars"]) == 0 for r in merged),
        "v1_quarantine": sum(r["v1_verdict"] == "quarantine" for r in merged),
        "v1_accept_gw": sum(r["v1_verdict"] == "accept_gw" for r in merged),
        "input_hash_unique": len({r["image_sha256"] for r in merged}),
    }
    (OUT / "auto_metrics.json").write_text(json.dumps(auto, ensure_ascii=False, indent=2), encoding="utf-8")

    review_cols = [
        "sample_id", "source_index", "gw_retrieval", "gw_fidelity", "gw_safety", "gw_coverage", "gw_structure",
        "vl_retrieval", "vl_fidelity", "vl_safety", "vl_coverage", "vl_structure",
        "source_anomaly", "winner", "vl_beneficial", "critical_field_notes", "review_notes",
    ]
    review_path = OUT / "human_review.tsv"
    if not review_path.exists():
        with review_path.open("w", encoding="utf-8", newline="") as handle:
            wr = csv.DictWriter(handle, fieldnames=review_cols, delimiter="\t")
            wr.writeheader()
            for row in merged:
                wr.writerow({"sample_id": row["sample_id"], "source_index": row["source_index"]})

    sections = ["# EXP-C1 run-01 — original / GW / VL / metrics review", "",
                "이 문서의 각 이미지는 두 엔진에 실제 전송된 JPEG와 동일하며 `features.tsv`의 SHA-256으로 검증된다.", ""]
    for row in merged:
        sample = sample_by_index[int(row["source_index"])]
        review_image = f"page_images/{row['sample_id']}.jpg"
        gw = (OUT / row["gw_file"]).read_text(encoding="utf-8")
        vl = (OUT / row["vl_file"]).read_text(encoding="utf-8")
        sections += [
            f"## {row['sample_id']} · source index {row['source_index']} · original p{row['page']}", "",
            f"- historical GW label: `{sample.get('historical_label_status', 'NOT_JOINED')}` — identity key required",
            f"- current v1: `{row['v1_verdict']}` — {row['v1_reason'] or 'no hard-fail reason'}",
            f"- chars: GW {row['gw_chars']} / VL {row['vl_chars']} · errors: GW `{row['gw_error'] or '-'}'` / VL `{row['vl_error'] or '-'}'`",
            f"- exact input: [`{review_image}`]({review_image}) · sha256 `{row['image_sha256']}`", "",
            f"![{row['sample_id']} original]({review_image})", "",
            "<details><summary>GW parser output</summary>", "", "```markdown", gw, "```", "</details>", "",
            "<details><summary>VL parser output</summary>", "", "```markdown", vl or "(EMPTY)", "```", "</details>", "",
        ]
    (OUT / "REVIEW.md").write_text("\n".join(sections), encoding="utf-8")


if __name__ == "__main__":
    main()
