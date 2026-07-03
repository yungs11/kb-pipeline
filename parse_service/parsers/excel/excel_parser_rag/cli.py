"""CLI (SoT §26).

사용:
    python -m excel_parser_rag parse \
      --input ./data/sample.xlsx \
      --output ./out/sample.chunks.jsonl \
      --report ./out/sample.report.md

--config 는 JSON 우선이며, PyYAML 이 설치된 환경에서만 YAML 을 허용한다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import ParserConfig

_SUBCOMMANDS = {"parse", "ingest", "search"}


def _str2bool(value: str) -> bool:
    v = str(value).strip().lower()
    if v in ("1", "true", "yes", "y", "on"):
        return True
    if v in ("0", "false", "no", "n", "off"):
        return False
    raise argparse.ArgumentTypeError(f"boolean 값이 아닙니다: {value!r}")


def _load_config_file(path: Path) -> Dict[str, Any]:
    """config 파일 로드 — JSON 우선, 실패 시 (가능하면) YAML."""
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:
            raise SystemExit(
                f"config 파일을 JSON 으로 파싱할 수 없고 PyYAML 도 설치되어 있지 않습니다: {path}"
            ) from exc
        data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SystemExit(f"config 파일 최상위는 객체(dict)여야 합니다: {path}")
    return data


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="excel_parser_rag",
        description="범용 Excel → RAG JSONL 파서 (SoT 기준 구현)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("parse", help="엑셀 파일을 파싱해 JSONL 청크를 생성한다")
    p.add_argument("--input", required=True, help="입력 .xlsx/.xlsm 파일 경로")
    p.add_argument("--output", default=None, help="출력 JSONL 경로 (기본: ./out/<stem>.chunks.jsonl)")
    p.add_argument("--report", default=None, help="markdown report 경로 (선택)")
    p.add_argument("--stats", default=None, help="stats JSON 파일 경로 (선택)")
    p.add_argument("--language", default=None, help="content_text 언어 (기본 ko)")
    p.add_argument("--include-hidden", type=_str2bool, default=None, metavar="BOOL",
                   help="숨김 행/시트 포함 여부 (true/false)")
    p.add_argument("--formula-mode", choices=("cached_value", "formula_text"), default=None,
                   help="수식 처리 모드")
    p.add_argument("--min-confidence", type=float, default=None,
                   help="이 미만 confidence chunk 는 drop")
    p.add_argument("--emit-debug", type=_str2bool, default=None, nargs="?", const=True, metavar="BOOL",
                   help="디버그 CSV 추가 출력")
    p.add_argument("--config", default=None, help="ParserConfig override 파일 (json, 가능 시 yaml)")
    p.add_argument("--backend", choices=("kordoc", "openpyxl"), default=None,
                   help="파싱 백엔드 (기본 kordoc)")
    p.add_argument("--kordoc-md", default=None, help="kordoc .md 파일 경로 (backend=kordoc)")
    p.add_argument("--kordoc-md-dir", default=None, help="<stem>.md 를 찾을 디렉토리 (backend=kordoc)")
    p.add_argument("--kordoc-bin", default=None, help="kordoc CLI 경로 (md 자동생성, Node 필요)")
    p.add_argument("--kordoc-md-out", default=None, help="자동생성 md 저장 디렉토리")

    ingest = sub.add_parser("ingest", help="JSONL 청크를 local hybrid index 에 적재한다")
    ingest.add_argument("--jsonl", required=True, help="parse 결과 JSONL 경로")
    ingest.add_argument("--index", default="./out/excel_docs.index.json", help="local index JSON 경로")
    ingest.add_argument("--provider", choices=("hashing", "bge-m3-http"), default="hashing",
                        help="embedding provider")
    ingest.add_argument("--endpoint", default=None, help="bge-m3-http endpoint URL")

    search = sub.add_parser("search", help="local hybrid index 를 검색한다")
    search.add_argument("--query", required=True, help="검색 질의")
    search.add_argument("--index", default="./out/excel_docs.index.json", help="local index JSON 경로")
    search.add_argument("--top-k", type=int, default=10, help="반환 개수")
    search.add_argument("--provider", choices=("hashing", "bge-m3-http"), default="hashing",
                        help="embedding provider")
    search.add_argument("--endpoint", default=None, help="bge-m3-http endpoint URL")
    search.add_argument("--dense-weight", type=float, default=0.35)
    search.add_argument("--sparse-weight", type=float, default=0.65)
    search.add_argument("--sheet", default=None, help="sheet payload filter")
    search.add_argument("--chunk-type", default=None, help="chunk_type payload filter")
    return parser


def _make_config(args: argparse.Namespace) -> ParserConfig:
    if args.config:
        config = ParserConfig.from_dict(_load_config_file(Path(args.config)))
    else:
        config = ParserConfig()
    if args.language is not None:
        config.language = args.language
    if args.include_hidden is not None:
        config.parse_hidden_rows = args.include_hidden
        config.parse_hidden_sheets = args.include_hidden
    if args.formula_mode is not None:
        config.formula_mode = args.formula_mode
    if args.min_confidence is not None:
        config.min_confidence = args.min_confidence
    if args.emit_debug is not None:
        config.emit_debug = bool(args.emit_debug)
    if getattr(args, "backend", None) is not None:
        config.backend = args.backend
    if getattr(args, "kordoc_md", None) is not None:
        config.kordoc_md_path = args.kordoc_md
    if getattr(args, "kordoc_md_dir", None) is not None:
        config.kordoc_md_dir = args.kordoc_md_dir
    if getattr(args, "kordoc_bin", None) is not None:
        config.kordoc_bin = args.kordoc_bin
    if getattr(args, "kordoc_md_out", None) is not None:
        config.kordoc_md_out = args.kordoc_md_out
    return config


def _run_parse(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"입력 파일이 없습니다: {input_path}", file=sys.stderr)
        return 2

    config = _make_config(args)

    from .backends import get_backend
    from .backends.base import BackendError
    from .emitters.csv_debug import write_debug_csv
    from .emitters.jsonl_emitter import write_jsonl
    from .emitters.markdown_report import write_report

    output_path = Path(args.output) if args.output else Path("./out") / f"{input_path.stem}.chunks.jsonl"

    try:
        chunks, stats = get_backend(config.backend).parse(input_path, config)
    except BackendError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    write_jsonl(chunks, output_path)
    stats["output_jsonl"] = str(output_path)

    if args.report:
        write_report(chunks, stats, args.report)
        stats["output_report"] = str(args.report)

    if config.emit_debug:
        debug_path = output_path.with_suffix(".debug.csv")
        write_debug_csv(chunks, debug_path)
        stats["output_debug_csv"] = str(debug_path)

    if args.stats:
        stats_path = Path(args.stats)
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        stats_path.write_text(
            json.dumps(stats, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        stats["output_stats"] = str(stats_path)

    print(json.dumps(stats, ensure_ascii=False, indent=2, default=str))
    return 0


def _make_embedder(args: argparse.Namespace):
    from .vector.bge_m3 import BgeM3HttpClient, HashingEmbeddingClient

    if args.provider == "hashing":
        return HashingEmbeddingClient()
    if not args.endpoint:
        raise SystemExit("--provider bge-m3-http 사용 시 --endpoint 가 필요합니다")
    return BgeM3HttpClient(args.endpoint)


def _run_ingest(args: argparse.Namespace) -> int:
    from .vector.ingest import ingest_jsonl

    stats = ingest_jsonl(args.jsonl, args.index, embedder=_make_embedder(args))
    print(json.dumps(stats, ensure_ascii=False, indent=2, default=str))
    return 0


def _run_search(args: argparse.Namespace) -> int:
    from .vector.search import search_index

    results = search_index(
        args.index,
        args.query,
        embedder=_make_embedder(args),
        top_k=args.top_k,
        dense_weight=args.dense_weight,
        sparse_weight=args.sparse_weight,
        filter_sheet=args.sheet,
        filter_chunk_type=args.chunk_type,
    )
    print(json.dumps({"query": args.query, "results": results}, ensure_ascii=False, indent=2, default=str))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 'parse' 가 기본 서브커맨드 — 옵션부터 시작하면 자동 보정
    if not argv or argv[0] not in _SUBCOMMANDS and argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv = ["parse"] + argv
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "parse":
        return _run_parse(args)
    if args.command == "ingest":
        return _run_ingest(args)
    if args.command == "search":
        return _run_search(args)
    parser.error(f"알 수 없는 명령: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

