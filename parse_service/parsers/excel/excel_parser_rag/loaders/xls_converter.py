"""레거시 .xls → .xlsx 변환기 (SoT §5.1).

openpyxl 은 .xls 를 읽지 못하므로, libreoffice(soffice) --headless 가
설치되어 있으면 그것으로 변환하고, 없으면 명확한 에러를 낸다.
외부 의존성(xlrd/pandas)은 사용하지 않는다 (SoT Rule 1).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

# macOS 기본 설치 경로 등 PATH 에 없을 수 있는 후보
_SOFFICE_CANDIDATES = (
    "soffice",
    "libreoffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/soffice",
    "/usr/local/bin/soffice",
    "/opt/homebrew/bin/soffice",
)

_CONVERT_TIMEOUT_SEC = 180


class XlsConversionError(RuntimeError):
    """.xls 변환 실패/불가 시 발생하는 예외."""


def find_soffice() -> Optional[str]:
    """libreoffice headless 실행 파일 경로를 찾는다. 없으면 None."""
    for candidate in _SOFFICE_CANDIDATES:
        if "/" in candidate:
            if Path(candidate).is_file():
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None


def convert_xls_to_xlsx(path: str | Path, output_dir: Optional[str | Path] = None) -> Path:
    """.xls 파일을 .xlsx 로 변환해 변환된 파일 경로를 반환한다.

    libreoffice --headless 가 없으면 XlsConversionError 를 던진다.
    output_dir 미지정 시 임시 디렉터리에 출력한다 (호출자가 정리 책임 없음 —
    OS 임시 영역이므로 파싱 동안만 유지되면 충분).
    """
    src = Path(path)
    if not src.is_file():
        raise XlsConversionError(f".xls 입력 파일이 존재하지 않습니다: {src}")

    soffice = find_soffice()
    if soffice is None:
        raise XlsConversionError(
            "레거시 .xls 파일은 libreoffice 변환이 필요하지만 'soffice'/'libreoffice' 실행 파일을 "
            "찾을 수 없습니다. libreoffice 를 설치하거나 (brew install --cask libreoffice), "
            "수동으로 .xlsx 로 저장한 뒤 다시 시도하세요."
        )

    out_dir = Path(output_dir) if output_dir is not None else Path(tempfile.mkdtemp(prefix="excel_parser_rag_xls_"))
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd: List[str] = [
        soffice,
        "--headless",
        "--norestore",
        "--convert-to",
        "xlsx",
        "--outdir",
        str(out_dir),
        str(src),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_CONVERT_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise XlsConversionError(
            f"libreoffice .xls 변환이 {_CONVERT_TIMEOUT_SEC}초 안에 끝나지 않았습니다: {src}"
        ) from exc
    except OSError as exc:
        raise XlsConversionError(f"libreoffice 실행 실패 ({soffice}): {exc}") from exc

    converted = out_dir / f"{src.stem}.xlsx"
    if proc.returncode != 0 or not converted.is_file():
        detail = (proc.stderr or proc.stdout or "").strip()
        raise XlsConversionError(
            f".xls → .xlsx 변환 실패 (exit={proc.returncode}): {src}"
            + (f"\nlibreoffice 출력: {detail[:500]}" if detail else "")
        )
    return converted
