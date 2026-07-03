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
