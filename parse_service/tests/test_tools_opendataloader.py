"""tools/opendataloader — PDF bytes → 페이지별 md 리스트 (ODL sentinel 분할)."""
import pytest
from parse_service.tools import ToolError
from parse_service.tools import opendataloader as odl


def test_split_pages_by_sentinel(monkeypatch):
    # opendataloader_pdf.convert 를 monkeypatch — md 1개 파일에 SEP 로 3페이지.
    def fake_convert(input_path, output_dir, **kw):
        import os
        with open(os.path.join(output_dir, "out.md"), "w", encoding="utf-8") as f:
            f.write(f"{odl.PAGE_SEP}page-1{odl.PAGE_SEP}page-2{odl.PAGE_SEP}page-3")
    monkeypatch.setattr(odl, "_odl_convert", fake_convert)
    pages = odl.convert_pdf_to_page_markdowns(b"%PDF-fake", "a.pdf")
    assert pages == ["page-1", "page-2", "page-3"]


def test_no_md_raises_toolerror(monkeypatch):
    monkeypatch.setattr(odl, "_odl_convert", lambda **kw: None)
    with pytest.raises(ToolError):
        odl.convert_pdf_to_page_markdowns(b"%PDF-fake", "a.pdf")


# ── 2026-08-11: 도구 경계 예외 계약 (Phase 1 §2) ─────────────────────────────
#
# `_odl_convert` 가 예외를 안 감싸 `_odl_lane` 의 `except ToolError` 를 뚫고
# `parse()` 전체가 500 으로 죽었다. 아래 두 케이스가 실제 실패 경로다.
# 위 기존 두 테스트는 `_odl_convert` **자체를** monkeypatch 하므로 이 변환 경로를
# 지나가지 않는다 — 그래서 여기서는 `opendataloader_pdf` 모듈을 갈아끼운다
# (함수 내부 import 라 `sys.modules` 주입이 먹는다).

import subprocess
import sys
import types

import pytest


def _fake_odl(convert_impl):
    m = types.ModuleType("opendataloader_pdf")
    m.convert = convert_impl
    return m


def test_called_process_error_becomes_toolerror(monkeypatch, tmp_path):
    """java 부재·JAR 실패 — 실측된 폐쇄망/개발환경 실패."""
    def boom(**kw):
        raise subprocess.CalledProcessError(1, ["java", "-jar", "odl.jar"])
    monkeypatch.setitem(sys.modules, "opendataloader_pdf", _fake_odl(boom))
    with pytest.raises(odl.ToolError, match="CalledProcessError"):
        odl.convert_pdf_to_page_markdowns(b"%PDF", "a.pdf")


def test_import_error_becomes_toolerror(monkeypatch):
    """`opendataloader-pdf` 가 pyproject dependencies 에 없어 `pip install .` 이미지에서
    빠진다 — PyMuPDF/docx/openpyxl 과 같은 폐쇄망 재발 패턴이라 **가장 잦은 실패 경로**다."""
    monkeypatch.setitem(sys.modules, "opendataloader_pdf", None)   # import → ImportError
    with pytest.raises(odl.ToolError, match="Error"):
        odl.convert_pdf_to_page_markdowns(b"%PDF", "a.pdf")


def test_parse_promotes_to_parser_error(monkeypatch):
    """도구 경계에서 ToolError 가 되면 `parse()` 는 ParserError 로 승격한다
    (raw subprocess/Import 예외가 새지 않는다)."""
    from parse_service.parsers import ParserError
    from parse_service.parsers import pdf as pdf_parser

    def boom(fb, fn):
        raise odl.ToolError("opendataloader 실행 실패: CalledProcessError: boom")
    monkeypatch.setattr(pdf_parser, "_page_markdowns", boom)
    monkeypatch.setattr(pdf_parser, "_safe_decide_route", lambda b: None)
    with pytest.raises(ParserError):
        pdf_parser.parse(b"%PDF", "a.pdf", ocr_url="")
