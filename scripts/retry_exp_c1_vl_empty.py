#!/usr/bin/env python3
"""Retry only the five empty VL results from EXP-C1 using saved input bytes."""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


REPO = Path(__file__).resolve().parents[1]
RUN = REPO / "research/EXP-C1-20260811/run-01"
OUT = RUN / "retries/vl-empty-retry-01"
IDS = ("I14", "I17", "I18", "I37", "I46")


def load_env(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key, value)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blocks_md(elements: list[dict]) -> str:
    chunks = []
    for element in elements or []:
        content = element.get("content") or {}
        chunks.append(content.get("markdown") or content.get("html") or content.get("text") or "")
    return "\n\n".join(chunk for chunk in chunks if chunk)


def main() -> None:
    load_env(REPO / "scripts/parse-svc.env")
    if not os.environ.get("MODEL_NAME"):
        raise SystemExit("MODEL_NAME must be explicit")
    sys.path.insert(0, str(REPO))
    from parse_service.parsers.ocr import ocr_elements_sync, prompts

    for name in ("page_images", "vl_raw", "normalized"):
        (OUT / name).mkdir(parents=True, exist_ok=True)

    feature_rows = {
        row["sample_id"]: row
        for row in __import__("csv").DictReader(
            (RUN / "features.tsv").open(encoding="utf-8"), delimiter="\t"
        )
    }
    prompt_pair = prompts.page_hybrid_prompts()

    def retry(sample_id: str) -> dict:
        prior = feature_rows[sample_id]
        source = RUN / prior["image_file"]
        jpeg = source.read_bytes()
        if sha256(jpeg) != prior["image_sha256"]:
            raise RuntimeError(f"saved input hash mismatch: {sample_id}")
        target = OUT / "page_images" / source.name
        shutil.copy2(source, target)

        started = time.monotonic()
        try:
            elements = ocr_elements_sync(jpeg, source.name, prompt_pair)
            markdown = blocks_md(elements)
            error = "" if elements and markdown.strip() else "empty"
        except Exception as exc:  # noqa: BLE001
            elements, markdown = [], ""
            error = f"{type(exc).__name__}: {str(exc)[:500]}"
        elapsed = round(time.monotonic() - started, 3)
        raw_path = OUT / "vl_raw" / source.with_suffix(".json").name
        md_path = OUT / "normalized" / f"{source.stem}_vl.md"
        raw_path.write_text(json.dumps(elements, ensure_ascii=False, indent=2), encoding="utf-8")
        md_path.write_text(markdown, encoding="utf-8")
        result = {
            "sample_id": sample_id,
            "source_image": str(source.relative_to(RUN)),
            "retry_image": str(target.relative_to(OUT)),
            "image_sha256": sha256(jpeg),
            "same_as_run01_input": True,
            "vl_seconds": elapsed,
            "vl_error": error,
            "vl_elements": len(elements),
            "vl_chars": len("".join(markdown.split())),
            "vl_output_sha256": sha256(markdown.encode()),
            "vl_raw_file": str(raw_path.relative_to(OUT)),
            "vl_file": str(md_path.relative_to(OUT)),
        }
        print(f"{sample_id} chars={result['vl_chars']} error={error or '-'} seconds={elapsed}", flush=True)
        return result

    with cf.ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(retry, IDS))
    results.sort(key=lambda row: row["sample_id"])

    manifest = {
        "experiment_id": "EXP-C1-20260811",
        "retry_id": "vl-empty-retry-01",
        "date": "2026-08-12",
        "git_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
        ).strip(),
        "parser_source": str(REPO),
        "reason": "user-requested retry of the five initial empty VL results",
        "sample_ids": list(IDS),
        "input_source": "run-01/page_images",
        "same_saved_image_bytes": True,
        "gw_recalled": False,
        "model_name": os.environ["MODEL_NAME"],
        "model_revision": os.environ.get("MODEL_REVISION", "provider-unavailable"),
        "temperature": 0.1,
        "max_tokens": int(os.environ.get("VL_MAX_TOKENS", "2000")),
        "guided_json": os.environ.get("USE_GUIDED_JSON", "1"),
        "guided_json_mode": os.environ.get("GUIDED_JSON_MODE", "extra_body"),
        "prompt": "page_hybrid_prompts",
        "prompt_sha256": sha256(
            json.dumps(prompt_pair, ensure_ascii=False, sort_keys=True).encode()
        ),
        "max_concurrency": 3,
        "submit_gap_seconds": 0,
        "results": results,
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
