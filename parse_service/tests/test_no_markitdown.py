"""markitdown 이 코드베이스에서 완전히 제거됐다(재유입 가드)."""
import os
import subprocess

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def test_no_markitdown_imports():
    # v2(리뷰 B8): Task 13 시점 범위 = parse_service + kb_pipeline.
    # (service/ 는 Task 14 에서 parsing.py 삭제 후 이 리스트에 "service" 를 추가한다.)
    # ^-anchored only (이 파일의 문자열 리터럴이 자기 매칭하지 않도록) + .py 한정(pyc 제외).
    r = subprocess.run(
        ["grep", "-rlE", "--include=*.py", r"^(from|import) markitdown\b",
         "parse_service", "kb_pipeline"],
        capture_output=True, text=True, cwd=_ROOT)
    assert r.stdout.strip() == "", f"markitdown imports remain: {r.stdout}"


def test_no_markitdown_in_requirements():
    txt = open(os.path.join(_ROOT, "requirements.txt")).read()
    assert "markitdown" not in txt
