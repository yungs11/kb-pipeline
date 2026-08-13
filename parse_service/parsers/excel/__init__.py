"""Excel 도메인 파서 — parse+chunk 결합(자체청킹) → chunk_needed=False.

Phase 2b: excel_parser_rag 를 in-process 로 직접 호출(HTTP 제거).
env: EXCEL_PARSER_BACKEND(기본 auto), KORDOC_BIN(기본 미설정), KORDOC_MD_OUT.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from parse_service.parsers import RouteResult, ParserError

#: csv 는 2026-08-11 편입 — 메모리상 xlsx 로 합성해 같은 백엔드로 흘린다(`csv_to_xlsx`).
EXCEL_EXTS = {"xlsx", "xlsm", "xls", "csv"}

#: OLE Compound File 시그니처 — 레거시 `.xls`(BIFF)의 컨테이너.
#: ⚠️ BIFF 전용이 아니다(구 .doc/.ppt, 암호화된 .xlsx 도 CFB). 정밀 판별은 비범위 —
#: 그런 입력은 변환을 시도했다가 실패한다(오늘은 openpyxl 이 즉시 실패한다).
_CFB_MAGIC = b"\xd0\xcf\x11\xe0"


def normalize_rag_chunk(rc: dict, index: int) -> dict | None:
    """RagChunk dict → facade 청크 {chunk_index, text, titles_context, pages}. 빈 텍스트면 None.

    facade service/excel_parser_client.py:normalize_rag_chunk 본문 byte-identical 이동.
    """
    text = (rc.get("content_text") or rc.get("title") or "").strip()
    if not text:
        return None
    title = rc.get("title")
    path = rc.get("path") or []
    titles_context = path or ([title] if title else None)
    return {"chunk_index": index, "text": text, "titles_context": titles_context, "pages": []}


def normalize_chunks(rag_chunks: list[dict]) -> list[dict]:
    out: list[dict] = []
    for rc in rag_chunks or []:
        norm = normalize_rag_chunk(rc, len(out))
        if norm is not None:
            out.append(norm)
    return out


def _fetch_rag_chunks(file_bytes: bytes, filename: str, excel_url: str | None = None) -> tuple[list[dict], dict]:
    """in-process: excel_parser_rag backend 직접 호출 (HTTP 제거).

    원본 excel-parser service/main.py:_run_parse 의 동기 파싱 본문 이식
    (임시파일 suffix, ParserConfig, get_backend(...).parse). backends 계약:
    get_backend(name).parse(input_path, config) -> (chunks: list[dict], stats: dict).
    excel_url 은 하위호환용 무시 파라미터(2e 에서 호출부와 함께 제거).

    반환: (raw_chunks, gate_summary) 튜플. gate_summary 는 raw 청크 + tmp_path 로
    in-process compute_gate_summary 로 계산(unlink 전). 계산 실패는 보수적 차단
    (spec §8 "추출 불가 = 적재 불가") 로 {"ok": False, "sheets": [], "error": ...}.
    """
    from parse_service.parsers.excel.excel_parser_rag.backends import get_backend
    from parse_service.parsers.excel.excel_parser_rag.config import ParserConfig
    from parse_service.parsers.excel.excel_parser_rag.gate import compute_gate_summary

    # 업로드 바이트는 안전한 임시 파일명으로 저장하지만, 파서가 그 임시 stem
    # (`excel_parser_…`)을 문서 제목으로 채택하면 청크 본문/검색어에 런타임 잡음이
    # 누출된다. :8600 excel-parser service 와 동일하게 원본 basename의 stem을
    # document_title로 명시한다. basename 정규화는 내부 직접 호출에도 경로 문자열이
    # 제목으로 섞이지 않게 하는 방어선이다.
    safe_filename = Path((filename or "upload.xlsx").replace("\x00", "")).name or "upload.xlsx"
    is_csv = Path(safe_filename).suffix.lower() == ".csv"
    if is_csv:
        # csv → xlsx 합성. document_title 은 아래에서 원본 stem 을 그대로 쓰므로
        # 파일명은 바꾸지 않고 바이트와 suffix 만 갈아끼운다.
        from parse_service.parsers.excel.csv_to_xlsx import csv_bytes_to_xlsx
        file_bytes = csv_bytes_to_xlsx(file_bytes, safe_filename)

    # ── 레거시 .xls(BIFF) → .xlsx (2026-08-13) ────────────────────────────────
    # **확장자가 아니라 매직바이트로 판정한다.** 확장자로 하면 두 방향으로 깨진다:
    #   · 이름만 .xls 인 xlsx(zip) — 지금은 openpyxl 이 zip 을 스니핑해 정상 처리되는데
    #     무조건 변환하면 soffice 없는 환경에서 되던 문서가 죽는다.
    #   · 이름이 .xlsx 인 진짜 BIFF — 확장자 기준으로는 안 고쳐진다.
    # 여기서 갈아끼우면 하류(전결 Tier1·계층 Tier1.5 확장자 게이트, kordoc 동반 워크북,
    # compute_gate_summary)가 전부 .xlsx 를 보게 되어 코드 변경 0 으로 해결된다.
    is_biff = file_bytes[:4] == _CFB_MAGIC
    if is_biff:
        from parse_service.parsers.excel.xls_to_xlsx import xls_bytes_to_xlsx
        file_bytes = xls_bytes_to_xlsx(file_bytes, safe_filename)

    # 강제 .xlsx 는 **바이트를 실제로 갈아끼운 경우에만**. zip 매직까지 조건에 넣으면
    # .xlsm(OOXML=zip) 의 임시파일 suffix 가 조용히 .xlsx 로 바뀐다(kordoc CLI 는
    # 확장자로 디스패치한다). 변환하지 않은 입력의 확장자는 하나도 바꾸지 않는다.
    suffix = ".xlsx" if (is_csv or is_biff) else (Path(safe_filename).suffix.lower() or ".xlsx")
    cfg_kwargs: dict = {
        # csv 는 백엔드를 openpyxl 로 고정한다. 기본 `auto` 는 "전결" 키워드(Tier1)나
        # 계층 지배도(Tier1.5)가 있을 때만 openpyxl 을 쓰고 그 외에는 kordoc 으로
        # 떨어지는데(backends/auto_backend.py), csv 유래 평면 표는 둘 다 아니다.
        # csv 에는 병합셀·다중시트·수식이 없어 kordoc 의 렌더 충실도 이점이 없고,
        # KORDOC_BIN 이 없는 환경에선 아예 실패한다(실측).
        "backend": "openpyxl" if is_csv else os.environ.get("EXCEL_PARSER_BACKEND", "auto"),
        "document_title": Path(safe_filename).stem,
    }
    if os.environ.get("KORDOC_BIN"):
        cfg_kwargs["kordoc_bin"] = os.environ["KORDOC_BIN"]
    if os.environ.get("KORDOC_MD_OUT"):
        cfg_kwargs["kordoc_md_out"] = os.environ["KORDOC_MD_OUT"]
    config = ParserConfig(**cfg_kwargs)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False, prefix="excel_parser_") as tmp:
        tmp.write(file_bytes)
        tmp_path = Path(tmp.name)
    try:
        chunks, _stats = get_backend(config.backend).parse(tmp_path, config)
        raw = [c if isinstance(c, dict) else c.__dict__ for c in (chunks or [])]
        try:
            gate_summary = compute_gate_summary(tmp_path, raw)
        except Exception as exc:  # noqa: BLE001 — gate 계산 실패는 보수적 차단(ok=False)
            gate_summary = {"ok": False, "sheets": [], "error": str(exc)}
        return raw, gate_summary
    finally:
        tmp_path.unlink(missing_ok=True)


def parse(file_bytes: bytes, filename: str, *, excel_url: str | None = None) -> RouteResult:
    try:
        rag_chunks, gate_summary = _fetch_rag_chunks(file_bytes, filename, excel_url)
    except ParserError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ParserError(f"excel parse failed for {filename}: {e}") from e
    chunks = normalize_chunks(rag_chunks)
    # 빈 청크 + 계산된 gate_summary 는 유효한 "깨진 엑셀 → 게이트가 reject" 결과다.
    # (예전엔 여기서 ParserError 를 던져 gate_summary 를 폐기했으나, 그러면 게이트가
    #  깨끗하게 reject 할 수 없다. raise 제거 — 다운스트림 게이트가 판정.)
    return RouteResult(kind="chunks", chunk_needed=False, chunks=chunks, gate_summary=gate_summary)
