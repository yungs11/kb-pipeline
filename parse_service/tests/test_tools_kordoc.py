"""tools/kordoc — CLI 래퍼: out.md 생성 확인, 실패 시 ToolError."""
import pytest
from parse_service.tools import ToolError
from parse_service.tools import kordoc


def test_cli_invocation_and_output(monkeypatch, tmp_path):
    def fake_run(cmd, **kw):
        # cmd = [bin, src, "--output", out, "--format", "markdown"]
        out = cmd[cmd.index("--output") + 1]
        with open(out, "w", encoding="utf-8") as f:
            f.write("# t\n<table><tr><td rowspan=\"2\">a</td></tr></table>")
        class R: returncode, stdout, stderr = 0, "", ""
        return R()
    monkeypatch.setattr(kordoc.subprocess, "run", fake_run)
    monkeypatch.setattr(kordoc.shutil, "which", lambda b: "/fake/kordoc")
    md = kordoc.convert_to_markdown(b"PK-docx", "a.docx")
    assert "<table>" in md and "rowspan" in md


def test_no_output_raises(monkeypatch):
    def fake_run(cmd, **kw):
        class R: returncode, stdout, stderr = 1, "", "boom"
        return R()
    monkeypatch.setattr(kordoc.subprocess, "run", fake_run)
    monkeypatch.setattr(kordoc.shutil, "which", lambda b: "/fake/kordoc")
    with pytest.raises(ToolError):
        kordoc.convert_to_markdown(b"PK", "a.docx")


def test_missing_binary_raises(monkeypatch):
    monkeypatch.setenv("KORDOC_BIN", "/definitely/not/here/kordoc")
    with pytest.raises(ToolError):
        kordoc.convert_to_markdown(b"PK", "a.docx")
