"""레거시 `.xls`(BIFF) 레인 — 입구 변환(§2.1) 회귀선.

**V1/V2/V2b 는 soffice 없이 돈다** — CI/맥에서 이 파일이 유일한 회귀선이다.
V3/V4 는 실제 변환이 필요해 skip 조건이 붙는다.

이 테스트들의 규칙: **실패할 수 있는 assert 만 쓴다.** (설계 검증 중 "가짜로 대체한
함수의 부작용을 검사해 항상 통과" 하는 형태를 다섯 번 잡았다.)
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from parse_service.parsers import excel as excel_parser
from parse_service.parsers.excel import xls_to_xlsx
from parse_service.parsers.excel.excel_parser_rag import backends, gate
from parse_service.parsers.excel.excel_parser_rag.loaders import xls_converter

FIXTURES = Path(__file__).parent / "fixtures"
CFB = b"\xd0\xcf\x11\xe0" + b"\x00" * 60
ZIP = b"PK\x03\x04" + b"\x00" * 60

_soffice = xls_converter.find_soffice()
needs_soffice = pytest.mark.skipif(_soffice is None, reason="soffice(LibreOffice) 필요")
needs_kordoc = pytest.mark.skipif(
    not os.environ.get("KORDOC_BIN"), reason="KORDOC_BIN 필요(auto → kordoc 경로)"
)


def _tmp_entries(prefix: str) -> set[str]:
    return {p.name for p in Path(tempfile.gettempdir()).glob(f"{prefix}*")}


class _CapturingBackend:
    """하류 스텁 — 임시파일 suffix 를 관측한다(test_parser_excel.py 관례)."""

    def __init__(self, sink):
        self.sink = sink

    def parse(self, input_path, config):
        self.sink["suffix"] = Path(input_path).suffix
        return ([{"content_text": "x", "title": "s", "path": ["s"]}], {})


@pytest.fixture
def lane(monkeypatch):
    """레인을 태우되 하류 백엔드/게이트는 스텁하고, 변환기 호출을 센다."""
    sink: dict = {"calls": 0}

    def fake_convert(file_bytes, _filename=None):
        sink["calls"] += 1
        return ZIP  # 변환됐다고 치고 zip 매직 반환

    monkeypatch.setattr(xls_to_xlsx, "xls_bytes_to_xlsx", fake_convert)
    monkeypatch.setattr(backends, "get_backend", lambda _n: _CapturingBackend(sink))
    # 스텁 백엔드의 가짜 임시파일을 게이트가 열면 error dict 노이즈가 난다(F10 경로).
    monkeypatch.setattr(gate, "compute_gate_summary", lambda _p, _c: {"ok": True, "sheets": []})
    return sink


# ── V1: 매직바이트 라우팅 (soffice 불필요) ──────────────────────────────────────
@pytest.mark.parametrize(
    "filename,payload,expect_calls,expect_suffix",
    [
        ("a.xls", CFB, 1, ".xlsx"),    # ① 이름 .xls + BIFF → 변환
        ("a.xlsx", CFB, 1, ".xlsx"),   # ② 이름 .xlsx + BIFF → 변환(확장자에 속지 않는다)
        ("a.xls", ZIP, 0, ".xls"),     # ③ 이름 .xls + zip → 변환 안 함, 확장자 보존
        ("a.xlsm", ZIP, 0, ".xlsm"),   # ④ .xlsm 이 .xlsx 로 바뀌지 않는다
        ("a.xls", b"col1\tcol2\n", 0, ".xls"),  # ⑤ 비CFB·비zip → 변환 안 함(비목표)
    ],
)
def test_magic_byte_routing(lane, filename, payload, expect_calls, expect_suffix):
    excel_parser.parse(payload, filename)
    assert lane["calls"] == expect_calls, f"{filename}: 변환기 호출 횟수"
    assert lane["suffix"] == expect_suffix, f"{filename}: 하류로 넘어간 임시파일 suffix"


# ── V2: 탈출구 — soffice 부재는 조용히 폴백하지 않는다 ─────────────────────────
def test_missing_soffice_raises_parser_error(monkeypatch):
    from parse_service.parsers import ParserError

    monkeypatch.setattr(xls_converter, "find_soffice", lambda: None)
    with pytest.raises(ParserError, match="soffice"):
        excel_parser.parse(CFB, "a.xls")


# ── V2b: 임시물 누수 — 어댑터 본체를 실제로 태운다 ────────────────────────────
def _fake_converter(src, output_dir=None):
    """받은 output_dir 에 유효 xlsx 를 써주는 가짜 변환기(soffice 없이 래퍼를 태운다)."""
    import openpyxl

    out = Path(output_dir) / f"{Path(src).stem}.xlsx"
    openpyxl.Workbook().save(out)
    return out


def test_adapter_leaves_no_tempdir(monkeypatch):
    monkeypatch.setattr(xls_to_xlsx, "convert_xls_to_xlsx", _fake_converter)
    before = _tmp_entries("xls2xlsx_")
    out = xls_to_xlsx.xls_bytes_to_xlsx(CFB, "a.xls")
    assert out[:4] == b"PK\x03\x04"
    assert _tmp_entries("xls2xlsx_") - before == set()


def test_adapter_cleans_up_on_failure(monkeypatch):
    def boom(src, output_dir=None):
        raise xls_converter.XlsConversionError("변환 실패(테스트)")

    monkeypatch.setattr(xls_to_xlsx, "convert_xls_to_xlsx", boom)
    before = _tmp_entries("xls2xlsx_")
    with pytest.raises(xls_converter.XlsConversionError):
        xls_to_xlsx.xls_bytes_to_xlsx(CFB, "a.xls")
    assert _tmp_entries("xls2xlsx_") - before == set(), "예외 경로에서도 finally 가 정리해야 한다"


# ── V3: 실제 왕복 · 충실도 · 게이트 (soffice 필요) ─────────────────────────────
# ⚠️ **kordoc 도 필요하다.** .xls 는 soffice 로 .xlsx 를 만든 뒤 엑셀 파서로 들어가는데,
#    `EXCEL_PARSER_BACKEND=auto` 기본값이 이 파일들을 kordoc 백엔드로 보낸다. soffice 만
#    검사하면 **soffice 는 있고 kordoc env 는 없는 머신에서 실패**한다(2026-08-14 실측:
#    KORDOC_BIN/KORDOC_MD_OUT 을 주면 12 passed, 없으면 이 둘만 실패).
@needs_soffice
@needs_kordoc
def test_real_xls_roundtrip_and_gate():
    before_lo = _tmp_entries("lo_")
    before_conv = _tmp_entries("excel_parser_rag_xls_")

    res = excel_parser.parse((FIXTURES / "legacy_sample.xls").read_bytes(), "legacy_sample.xls")

    assert res.chunks, "변환 후에도 청크가 나와야 한다"
    body = "\n".join(c["text"] for c in res.chunks)
    assert "임대료" in body and "관리비" in body, "값이 보존돼야 한다"

    gs = res.gate_summary
    assert gs is not None
    assert gs.get("error") is None, f"게이트가 예외로 죽었다: {gs.get('error')}"
    # `ok` 자체는 내용 판정이다(이 픽스처는 헤더가 없어 unclear_header 로 ok=False 가 정상).
    # 여기서 증명할 것은 **게이트가 변환본을 실제로 열어 시트를 판정했다**는 것.
    assert gs["sheets"] and all("findings" in s for s in gs["sheets"]), \
        f"게이트가 시트를 판정하지 못했다: {gs}"
    assert not any(f["code"] == "ref_error" for s in gs["sheets"] for f in s["findings"]), \
        "정상 픽스처에 참조오류가 생겼다(변환이 수식을 깨뜨렸다)"

    # 실 변환기가 남긴 임시물이 없어야 한다(프로필·미지정 output_dir 양쪽).
    assert _tmp_entries("lo_") - before_lo == set(), "soffice 프로필이 남았다"
    assert _tmp_entries("excel_parser_rag_xls_") - before_conv == set(), \
        "output_dir 없이 변환기를 부른 호출처가 있다"


@needs_soffice
def test_merged_cells_survive_conversion():
    """병합셀 충실도 — kordoc 산출물의 colspan 으로 관측한다(모호한 신호 금지)."""
    from parse_service.parsers.excel.xls_to_xlsx import xls_bytes_to_xlsx

    if not shutil.which(os.environ.get("KORDOC_BIN", "kordoc")):
        pytest.skip("kordoc 필요")

    tmp = Path(tempfile.mkdtemp(prefix="mergecmp_"))
    try:
        src_xls = FIXTURES / "legacy_sample.xls"
        conv = tmp / "conv.xlsx"
        conv.write_bytes(xls_bytes_to_xlsx(src_xls.read_bytes(), src_xls.name))

        def colspans(path: Path) -> int:
            import subprocess

            out = tmp / (path.stem + ".md")
            subprocess.run([os.environ.get("KORDOC_BIN", "kordoc"), str(path), "-o", str(out),
                            "--silent"], check=True, capture_output=True, timeout=180)
            return out.read_text(encoding="utf-8").count("colspan")

        assert colspans(conv) == colspans(src_xls), "변환본이 원본 대비 병합셀을 잃었다"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@needs_soffice
@needs_kordoc
def test_cached_ref_error_survives_roundtrip():
    """§2.4 오검 축 — 캐시값에만 있는 #REF! 가 재계산으로 지워지면 실패한다.

    H3=`=J9`(수식에 토큰 없음) 이므로 **캐시값 경로로만** 게이트에 잡힌다.
    통상적인 `=SUM(#REF!)` 픽스처를 쓰면 수식 스캔이 무조건 잡아 이 검증이 무의미해진다.
    """
    res = excel_parser.parse((FIXTURES / "broken_formula.xls").read_bytes(), "broken_formula.xls")
    gs = res.gate_summary
    assert gs is not None and gs.get("error") is None

    ref_cells = [
        cell
        for sheet in gs["sheets"]
        for f in sheet.get("findings", [])
        if f.get("code") == "ref_error"
        for cell in f.get("cells", [])
    ]
    assert "H3" in ref_cells, (
        f"H3(캐시값 전용 #REF!)가 게이트에서 사라졌다 — 불량 문서가 조용히 통과한다. "
        f"관측된 ref cells={ref_cells}"
    )


# ── V4: 전결 회귀(F4) — 변환 후 openpyxl(delegation) 레인으로 가는가 ──────────
@needs_soffice
def test_delegation_xls_reaches_openpyxl_gate():
    """F4 회귀 — 확장자 게이트가 `.xls` 를 **전결 검사 전에** 잘라내지 않는가.

    `_should_try_openpyxl` 은 `suffix in {".xlsx",".xlsm"} and detect_delegation_keyword(...)`
    라 `and` 단락 평가로 `.xls` 는 키워드 검사에 **도달조차 못 했다**. 변환 후 도달한다.

    ⚠️ 여기서 Tier2 수락(`routed_backend == "openpyxl"` + `delegation_rule` 청크)까지
    검증하지 않는 이유: 그건 문서 **구조**에 달렸고(합성 픽스처로는 `delegation_rule` 이
    생성되지 않는다), 이 변경이 책임지는 범위가 아니다. 실제 위임전결 문서로 닫는 항목은
    plan §7 로 넘겼다. 관측 가능한 것만 assert 한다.
    """
    from parse_service.parsers.excel.excel_parser_rag.backends.auto_backend import (
        _should_try_openpyxl,
    )
    from parse_service.parsers.excel.xls_to_xlsx import xls_bytes_to_xlsx

    tmp = Path(tempfile.mkdtemp(prefix="deleg_"))
    try:
        src = FIXTURES / "delegation_sample.xls"
        assert _should_try_openpyxl(src) is False, \
            "전제가 깨졌다 — 원본 .xls 는 확장자 게이트에 막혀야 한다(F4)"

        conv = tmp / "delegation_sample.xlsx"
        conv.write_bytes(xls_bytes_to_xlsx(src.read_bytes(), src.name))
        assert _should_try_openpyxl(conv) is True, (
            "변환 후에도 전결 레인에 도달하지 못한다 — 변환이 '전결' 키워드를 잃었거나 "
            "확장자가 바뀌지 않았다"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
