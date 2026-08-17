"""OpenDataLoader PDF 도구 — PDF bytes → 페이지별 markdown 리스트.

opendataloader_pdf(JRE 21) 를 subprocess 로 부른다. 문서당 .md 1개가 나오고
``markdown_page_separator`` 로 페이지 앞에 sentinel 이 삽입된다 → split 해 복원.
"""
from __future__ import annotations

import glob
import os
import re
import tempfile

from parse_service.tools import ToolError

#: 콘텐츠에 나타날 일 없는 페이지 sentinel (기존 parsing.py:_PAGE_SEP 그대로).
PAGE_SEP = "<<<ODL_PAGE_BREAK>>>"


def _safe_basename(name: str) -> str:
    base = os.path.basename((name or "").replace("\\", "/")).replace("\x00", "")
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base) or "upload"
    return "_" + base if base.startswith(".") else base


def _odl_convert(*, input_path: str, output_dir: str) -> None:
    """ODL CLI 실행. **도구 경계에서 모든 실패를 `ToolError` 로 변환한다.**

    2026-08-11: 여기서 예외를 안 감싸 계약이 깨져 있었다 — `_odl_lane` 은 `except ToolError`
    로만 받는데 실제로는 아래 둘이 그대로 올라와 **`parse()` 전체가 500 으로 죽었다**.

      · `ImportError`  — `opendataloader-pdf` 는 `requirements.txt` 에만 있고
        `pyproject.toml` dependencies 엔 없다. `pip install .` 로 만든 이미지에서 빠진다
        (PyMuPDF/docx/openpyxl 이 같은 이유로 폐쇄망에서만 터진 전례가 있다).
      · `subprocess.CalledProcessError` — java 부재/JAR 실패. 실측 확인.

    그래서 **import 도 try 안**에 둔다. `except Exception` 이 넓어 보이지만 try 본문이
    import 와 `convert` 호출 둘뿐이라 삼킬 정상 흐름 예외가 없다.
    """
    try:
        import opendataloader_pdf

        opendataloader_pdf.convert(
            input_path=input_path, output_dir=output_dir, format="markdown",
            markdown_with_html=True, markdown_page_separator=PAGE_SEP, quiet=True,
        )
    except Exception as e:  # noqa: BLE001 — 도구 경계: 원인을 ToolError 로 승격
        raise ToolError(f"opendataloader 실행 실패: {type(e).__name__}: {e}") from e


def _collect_extracted_images(tmp: str) -> dict[str, bytes]:
    """§B.1 — ODL 이 크롭해 `tmp`(추출 산출물 폴더, `.md` 와 같은 곳)에 써낸 이미지 파일들을
    tmp 삭제 전에 바이트로 확보한다. 마크다운의 `<파일명>_images/imageFileN.png` 참조와
    매칭시키기 위해 **tmp 기준 상대경로**(posix 슬래시)를 키로 쓴다.

    실패는 이 함수 안에서 흡수(빈 dict 반환) — ODL 텍스트 추출(`convert_pdf_to_page_markdowns`
    의 본 목적) 은 이미지 확보 실패와 무관하게 항상 성공해야 한다("네이티브 텍스트 레인은
    안 바꾼다"는 하드 제약, CLAUDE.md 워크플로 참고).
    """
    try:
        images: dict[str, bytes] = {}
        for path in glob.glob(os.path.join(tmp, "**", "*_images", "*"), recursive=True):
            if not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, tmp).replace(os.sep, "/")
            with open(path, "rb") as fh:
                images[rel] = fh.read()
        return images
    except Exception:  # noqa: BLE001 — 이미지 확보는 비치명, 텍스트 레인 무영향
        return {}


def convert_pdf_to_page_markdowns(
    file_bytes: bytes, filename: str,
) -> tuple[list[str], dict[str, bytes]]:
    """PDF → (페이지별 markdown 리스트, ODL이 추출한 이미지 파일 {상대경로: bytes}).

    두 번째 원소는 §B.2(ODL 이미지 면적필터+VL요약)가 소비한다 — 호출부가 이미지를
    안 쓰면(구 시그니처 기대) 튜플 언패킹이 필요하다: `md_texts, _images = convert_...(...)`.
    """
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, _safe_basename(filename))
        if os.path.commonpath([os.path.realpath(tmp), os.path.realpath(src)]) != os.path.realpath(tmp):
            raise ToolError("unsafe filename")
        with open(src, "wb") as fh:
            fh.write(file_bytes)
        _odl_convert(input_path=src, output_dir=tmp)
        mds = sorted(glob.glob(os.path.join(tmp, "**", "*.md"), recursive=True))
        if not mds:
            raise ToolError(f"opendataloader produced no md for {filename}")
        full = PAGE_SEP.join(
            open(m, encoding="utf-8", errors="replace").read() for m in mds
        )
        md_texts = full.split(PAGE_SEP)
        if len(md_texts) > 1 and not md_texts[0].strip():
            md_texts = md_texts[1:]
        images = _collect_extracted_images(tmp)   # tmp 삭제(with 블록 종료) 전에 확보
        return md_texts, images
