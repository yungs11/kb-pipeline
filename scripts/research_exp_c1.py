#!/usr/bin/env python3
"""EXP-C1: identical saved page JPEG -> current GW and VL parser modules.

The experiment is deliberately resumable.  The page JPEG is written and hashed
before either network call, so the exact parser input remains auditable.
"""
from __future__ import annotations

import csv
import concurrent.futures as cf
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unicodedata


REPO = Path(__file__).resolve().parents[1]
COMPARE = Path(os.environ.get("KBP_COMPARE_ROOT", "/Users/xxx/workspace/9.kbp-parser-compare"))
EXP = Path(os.environ.get("KBP_EXP_DIR", REPO / "research/EXP-C1-20260811"))
SAMPLE_SOURCE = COMPARE / "data/samples/qual_sample_60p_normal.tsv"
DEFAULT_SELECTED = (3, 10, 14, 17, 18, 19, 20, 24, 26, 35, 37, 41, 42, 46, 49, 56)
SELECTED = tuple(int(x) for x in os.environ.get(
    "KBP_EXP_SELECTED", ",".join(map(str, DEFAULT_SELECTED))).split(",") if x)
DPI = 150

TAG_RE = re.compile(r"<[^>]+>")
HANGUL_RE = re.compile(r"[가-힣]")
HAN_RE = re.compile(r"[一-鿿]")
HEAD_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S", re.M)
CRITICAL_RE = re.compile(
    r"(?:\d{1,3}(?:,\d{3})+|\d{4}[.년/-]\s*\d{1,2}|\d{4}(?:가|나|다|라|마|바|사|아|자|차|카|타|파|하)[가-힣]*\d+|제\s*\d+\s*조)"
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def slug(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]+", "_", text).strip("_")[:48]


def normalized_name(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]+", "", unicodedata.normalize("NFC", text)).lower()


def resolve_source(row: dict, pdf_index: dict[str, list[Path]]) -> Path:
    """Resolve stale historical paths against the copied test-documents tree."""
    original = Path(row["src"])
    if original.exists():
        return original
    matches = pdf_index.get(normalized_name(row["doc"]), [])
    if len(matches) != 1:
        raise RuntimeError(f"source resolution expected 1 match, got {len(matches)}: {row['doc']}")
    return matches[0]


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key, value)


def visible(text: str) -> str:
    return re.sub(r"\s+", "", TAG_RE.sub("", text or ""))


def repeat_features(text: str) -> dict:
    compact = visible(text)
    out = {}
    for n in (2, 3, 4, 5):
        grams = [compact[i:i+n] for i in range(max(0, len(compact) - n + 1))]
        if not grams:
            out[f"top{n}_ratio"] = 0.0
            continue
        counts = {}
        for gram in grams:
            counts[gram] = counts.get(gram, 0) + 1
        out[f"top{n}_ratio"] = round(max(counts.values()) / len(grams), 4)
    return out


def text_features(text: str) -> dict:
    compact = visible(text)
    hangul = len(HANGUL_RE.findall(compact))
    han = len(HAN_RE.findall(compact))
    return {
        "chars": len(compact),
        "hangul": hangul,
        "han": han,
        "cjk_ratio": round(han / max(1, han + hangul), 4),
        "headings": len(HEAD_RE.findall(text or "")),
        "tables": (text or "").lower().count("<table"),
        "critical_patterns": len(CRITICAL_RE.findall(text or "")),
        **repeat_features(text),
    }


def blocks_md(elements: list[dict]) -> str:
    chunks = []
    for element in elements or []:
        content = element.get("content") or {}
        chunks.append(content.get("markdown") or content.get("html") or content.get("text") or "")
    return "\n\n".join(x for x in chunks if x)


def git_sha() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()


def main() -> None:
    sys.path.insert(0, str(REPO))
    load_env(REPO / "scripts/parse-svc.env")
    if not os.environ.get("MODEL_NAME"):
        raise SystemExit("MODEL_NAME must be explicit")

    import pymupdf as fitz
    import logging
    from kb_pipeline.blockify import hybrid_to_blocks
    from parse_service.pdf_pages import render_pdf_pages
    from parse_service.parsers.degen_filter import assess_page
    from parse_service.parsers.ocr import ocr_elements_sync, prompts
    from parse_service.parsers.pdf import paddle_gw
    from parse_service.parsers.pdf.page_verdict import judge_page

    logging.getLogger("kb_pipeline.parse_service.parsers.ocr").setLevel(logging.CRITICAL)
    logging.getLogger("parse_service.parsers.ocr").setLevel(logging.CRITICAL)

    for name in ("page_images", "gw_raw", "vl_raw", "normalized"):
        (EXP / name).mkdir(parents=True, exist_ok=True)

    all_rows = list(csv.DictReader(SAMPLE_SOURCE.open(encoding="utf-8"), delimiter="\t"))
    pdf_index: dict[str, list[Path]] = {}
    for path in (COMPARE / "data/test-documents").rglob("*.pdf"):
        pdf_index.setdefault(normalized_name(path.name), []).append(path)
    rows = [(idx, all_rows[idx - 1]) for idx in SELECTED]

    sample_cols = ["sample_id", "source_index", "historical_label_status", "doc", "page", "src"]
    with (EXP / "sample.tsv").open("w", encoding="utf-8", newline="") as handle:
        wr = csv.DictWriter(handle, fieldnames=sample_cols, delimiter="\t")
        wr.writeheader()
        for pos, (idx, row) in enumerate(rows, 1):
            wr.writerow({
                "sample_id": f"S{pos:02d}", "source_index": idx,
                "historical_label_status": "NOT_JOINED_IDENTITY_KEY_REQUIRED",
                "doc": row["doc"], "page": row["page"],
                "src": str(resolve_source(row, pdf_index)),
            })

    prompt_pair = prompts.page_hybrid_prompts()
    prompt_hash = sha256(json.dumps(prompt_pair, ensure_ascii=False, sort_keys=True).encode())
    manifest = {
        "experiment_id": "EXP-C1-20260811", "date": "2026-08-11",
        "git_sha": git_sha(), "parser_source": str(REPO), "render_dpi": DPI,
        "same_image_bytes_for_both_engines": True,
        "sample_source": str(SAMPLE_SOURCE), "sample_indices": list(SELECTED),
        "split": "DEV", "selection_method": "convenience sample; no positional historical-label join",
        "model_name": os.environ["MODEL_NAME"],
        "model_revision": os.environ.get("MODEL_REVISION", "provider-unavailable"),
        "temperature": 0.1, "max_tokens": int(os.environ.get("VL_MAX_TOKENS", "2000")),
        "guided_json": os.environ.get("USE_GUIDED_JSON", "1"),
        "guided_json_mode": os.environ.get("GUIDED_JSON_MODE", "extra_body"),
        "prompt": "page_hybrid_prompts", "prompt_sha256": prompt_hash,
        "gw_endpoint": os.environ.get("KBP_PADDLE_OCR_GATEWAY_URL", ""),
        "gw_version": "endpoint-does-not-expose-revision",
        "vl_endpoint_host": re.sub(r"(?<=//)[^/]+", "<provider>", os.environ.get("MODEL_API_URL", "")),
        "human_review_policy": "16/16 original visual review; critical-field spot check",
    }
    (EXP / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    feature_rows = []
    for pos, (idx, row) in enumerate(rows, 1):
        sid = f"S{pos:02d}"
        stem = f"{sid}_{slug(row['doc'])}_p{row['page']}"
        image_path = EXP / "page_images" / f"{stem}.jpg"
        gw_path = EXP / "gw_raw" / f"{stem}.md"
        vl_json_path = EXP / "vl_raw" / f"{stem}.json"
        vl_md_path = EXP / "normalized" / f"{stem}_vl.md"

        src = resolve_source(row, pdf_index)
        doc = fitz.open(src)
        one = fitz.open()
        one.insert_pdf(doc, from_page=int(row["page"]) - 1, to_page=int(row["page"]) - 1)
        jpeg = render_pdf_pages(one.tobytes(), dpi=DPI)[0].jpeg
        one.close(); doc.close()
        if image_path.exists() and image_path.read_bytes() != jpeg:
            raise RuntimeError(f"saved image drift: {image_path}")
        image_path.write_bytes(jpeg)  # persisted before calls

        def call_gw():
            t0 = time.monotonic()
            try:
                value = paddle_gw._post_page(jpeg, image_path.name)
                error = ""
            except Exception as exc:  # noqa: BLE001
                value, error = "", f"{type(exc).__name__}: {str(exc)[:200]}"
            return value, error, time.monotonic() - t0

        saved_elements = (json.loads(vl_json_path.read_text(encoding="utf-8"))
                          if vl_json_path.exists() else [])
        def call_vl():
            t0 = time.monotonic()
            try:
                value = ocr_elements_sync(jpeg, image_path.name, prompt_pair)
                md = blocks_md(value)
                error = "" if value else "empty"
            except Exception as exc:  # noqa: BLE001
                value, md = [], ""
                error = f"{type(exc).__name__}: {str(exc)[:200]}"
            return value, md, error, time.monotonic() - t0

        gw_cached = gw_path.exists() and gw_path.stat().st_size
        vl_cached = bool(saved_elements)
        with cf.ThreadPoolExecutor(max_workers=2) as executor:
            gw_future = None if gw_cached else executor.submit(call_gw)
            vl_future = None if vl_cached else executor.submit(call_vl)
            if gw_cached:
                gw_md, gw_err, gw_s = gw_path.read_text(encoding="utf-8"), "", 0.0
            else:
                gw_md, gw_err, gw_s = gw_future.result()
                gw_path.write_text(gw_md, encoding="utf-8")
            if vl_cached:
                elements, vl_md, vl_err, vl_s = (
                    saved_elements, vl_md_path.read_text(encoding="utf-8"), "", 0.0)
            else:
                elements, vl_md, vl_err, vl_s = vl_future.result()
            vl_json_path.write_text(json.dumps(elements, ensure_ascii=False, indent=2), encoding="utf-8")
            vl_md_path.write_text(vl_md, encoding="utf-8")

        page = {"page_number": 1, "blocks": hybrid_to_blocks(gw_md, page_idx=1),
                "status": "error" if gw_err else "ok", "error": gw_err}
        assessment = assess_page(page)
        verdict = judge_page(page, assessment, is_diagram=False, ink_fn=lambda: float(row["ink"]))
        gf, vf = text_features(gw_md), text_features(vl_md)
        record = {
            "sample_id": sid, "source_index": idx, "page": row["page"],
            "image_file": str(image_path.relative_to(EXP)), "image_sha256": sha256(jpeg),
            "image_bytes": len(jpeg), "gw_error": gw_err, "vl_error": vl_err,
            "gw_seconds": round(gw_s, 3), "vl_seconds": round(vl_s, 3),
            "v1_verdict": verdict.verdict.value, "v1_reason": verdict.reason,
            "v1_signals_json": json.dumps(verdict.signals, ensure_ascii=False, sort_keys=True),
            **{f"gw_{k}": v for k, v in gf.items()},
            **{f"vl_{k}": v for k, v in vf.items()},
            "gw_output_sha256": sha256(gw_md.encode()), "vl_output_sha256": sha256(vl_md.encode()),
        }
        feature_rows.append(record)
        print(f"{sid} idx={idx} GW={gf['chars']} VL={vf['chars']} verdict={record['v1_verdict']}", flush=True)

    cols = list(feature_rows[0])
    with (EXP / "features.tsv").open("w", encoding="utf-8", newline="") as handle:
        wr = csv.DictWriter(handle, fieldnames=cols, delimiter="\t")
        wr.writeheader(); wr.writerows(feature_rows)
    (EXP / "auto_metrics.json").write_text(json.dumps({
        "N": len(feature_rows),
        "gw_engine_errors": sum(bool(r["gw_error"]) for r in feature_rows),
        "vl_engine_errors": sum(bool(r["vl_error"]) for r in feature_rows),
        "v1_quarantine": sum(r["v1_verdict"] == "quarantine" for r in feature_rows),
        "v1_engine_error": sum(r["v1_verdict"] == "engine_error" for r in feature_rows),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
