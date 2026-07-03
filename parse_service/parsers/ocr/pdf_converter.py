"""PDF 변환/렌더 — document-parser converter/pdf_utilities.py + handlers/pdf_handler.py 이식 (Phase 2c).

- convert_to_pdf_bytes / is_convertible_to_pdf: Office 문서 → PDF (Gotenberg
  ``/forms/libreoffice/convert``). 원본은 gotenberg.converter.PDFConverter 를 경유하나,
  그 실체는 위 endpoint 로의 multipart POST — httpx 직접 호출로 이식(시그니처/반환 계약 동일:
  ``(pdf_bytes|None, ok, saved_name|None)``, 실패는 예외가 아닌 ``(None, False, None)``).
- pdf_bytes_to_base64_list: PDF → 페이지별 PNG base64. 원본은 converter/image_converter
  (safe_fitz) 경유 — 동일 렌더 엔진 PyMuPDF(fitz) 직접 사용으로 이식
  (parse_service/pdf_pages.py 와 같은 패턴, env ``IMAGE_CONVERSION_DPI`` 기본 450).
치환(plan 이식 규칙): config → env, save_intermediate_files/redis/minio 경로 삭제.
"""

import io
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# 원본 core/config/settings.py GotenbergConfig.supported_mime_types
SUPPORTED_OFFICE_MIME_TYPES: Dict[str, str] = {
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "odt": "application/vnd.oasis.opendocument.text",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
}

_GOTENBERG_TIMEOUT = float(os.environ.get("GOTENBERG_TIMEOUT", "120"))


class PDFConversionError(RuntimeError):
    """PDF 변환 실패 예외 (원본 pdf_handler.PDFConversionError)."""


def is_convertible_to_pdf(file_path: str) -> bool:
    """파일이 PDF로 변환 가능한지 확인합니다."""
    if not os.path.exists(file_path):
        return False

    file_ext = Path(file_path).suffix.lower()
    if file_ext == '.pdf':
        return True

    return file_ext.lstrip('.') in SUPPORTED_OFFICE_MIME_TYPES


def convert_to_pdf_bytes(
    file_path: str,
    gotenberg_url: Optional[str] = None,
    libreoffice_options: Optional[Dict[str, Any]] = None,
    page_range: Optional[str] = None,
) -> Tuple[Optional[bytes], bool, Optional[str]]:
    """파일을 PDF 바이트 데이터로 변환합니다.

    Returns:
        (변환된 PDF 바이트, 성공 여부, 저장된 PDF 파일명) 튜플 — 실패 시 (None, False, None).
    """
    try:
        path = Path(file_path)
        if not path.exists():
            logger.error(f"Source file not found: {file_path}")
            return None, False, None

        file_ext = path.suffix.lower()
        if file_ext == '.pdf':
            logger.info(f"Source file is already PDF: {file_path}")
            return path.read_bytes(), True, path.stem

        file_ext_clean = file_ext.lstrip('.')
        mime = SUPPORTED_OFFICE_MIME_TYPES.get(file_ext_clean)
        if mime is None:
            logger.warning(f"Unsupported file format: {file_ext}")
            return None, False, None

        base = (gotenberg_url or os.environ.get("GOTENBERG_URL", "http://localhost:3000")).rstrip("/")
        data: Dict[str, Any] = {}
        if page_range:
            data["nativePageRanges"] = page_range
        if libreoffice_options:
            data.update({k: str(v) for k, v in libreoffice_options.items()})

        logger.info(f"Converting source file to PDF bytes: {file_path}")
        with path.open("rb") as f:
            resp = httpx.post(
                f"{base}/forms/libreoffice/convert",
                files={"files": (path.name, f, mime)},
                data=data or None,
                timeout=_GOTENBERG_TIMEOUT,
            )
        if resp.status_code != 200:
            logger.error(
                f"PDF conversion failed: HTTP {resp.status_code} — {resp.text[:300]}"
            )
            return None, False, None

        pdf_content = resp.content
        logger.info(f"Generated PDF bytes: {len(pdf_content)} bytes")
        return pdf_content, True, None

    except Exception as e:  # noqa: BLE001 — 원본 계약: 실패는 (None, False, None)
        logger.error(f"Unexpected error during PDF conversion: {e}")
        return None, False, None


def pdf_bytes_to_base64_list(
    pdf_bytes: bytes,
    output_format: str = "png",
    pdf_filename: str = None,
    use_streaming: bool = False,
    page_range: Optional[str] = None,
) -> List[str]:
    """PDF 바이트 데이터를 base64 이미지 목록으로 변환합니다.

    Returns:
        페이지 순서대로 정렬된 base64 문자열 리스트

    Raises:
        PDFConversionError: PDF 변환 기능이 없거나 변환에 실패한 경우
    """
    try:
        import fitz  # PyMuPDF
    except Exception as e:  # noqa: BLE001
        raise PDFConversionError("PDF to image conversion not available") from e

    if output_format and output_format.lower() != "png":
        raise PDFConversionError(
            f"Unsupported output format requested: {output_format}. Only PNG is supported."
        )

    # 페이지 범위 파싱 (1-based first/last)
    first_page = last_page = None
    if page_range:
        from parse_service.parsers.ocr.image_utils import parse_page_range
        nums = parse_page_range(page_range)
        if nums:
            first_page, last_page = nums[0], nums[-1]

    dpi = int(os.environ.get("IMAGE_CONVERSION_DPI", "450"))
    b64_list: List[str] = []
    try:
        import base64
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for i, page in enumerate(doc):
                page_num = i + 1
                if first_page is not None and page_num < first_page:
                    continue
                if last_page is not None and page_num > last_page:
                    break
                pix = page.get_pixmap(dpi=dpi, alpha=False)
                b64_list.append(base64.b64encode(pix.tobytes(output="png")).decode("utf-8"))
    except PDFConversionError:
        raise
    except Exception as e:  # noqa: BLE001
        raise PDFConversionError(f"Failed to convert PDF to image: {e}") from e

    if not b64_list:
        raise PDFConversionError("Failed to convert PDF to image")

    return b64_list
