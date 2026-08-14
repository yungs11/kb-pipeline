"""레거시 .xls → .xlsx 변환기 (SoT §5.1).

openpyxl 은 .xls 를 읽지 못하므로, libreoffice(soffice) --headless 가
설치되어 있으면 그것으로 변환하고, 없으면 명확한 에러를 낸다.
외부 의존성(xlrd/pandas)은 사용하지 않는다 (SoT Rule 1).

⚠️ 2026-08-13 이전까지 이 모듈은 **실효적으로 죽은 코드**였다 — 유일한 호출처가
openpyxl 백엔드 전용 경로(`workbook_loader`)였는데 `.xls` 는 `auto_backend` 의 확장자
게이트에 막혀 kordoc 으로 라우팅됐고, 거기서 동반 openpyxl 읽기가 먼저 죽었다.
이제 **엑셀 레인 입구**(`parsers/excel/__init__.py`)가 CFB 매직을 보고 바이트를
갈아끼우므로 하류 전체가 `.xlsx` 만 본다.

**프로세스/디렉터리 수명 규약**
- 프로필 디렉터리(`lo_*`)는 **변환 1회당 새로 만들고** `finally` 에서 **그것만** 지운다.
  `output_dir` 은 **절대 건드리지 않는다**(호출자 소유 — 산출물이 거기 있다).
- 프로필을 `output_dir` 안에 중첩시키지 않는다: ① 호출자의 산출물 탐색(유일한 `*.xlsx`)에
  프로필 트리가 섞이고, ② 잔존 검사(`gettempdir()` 직하 `lo_*`)가 무력화된다.
- 타임아웃 시 **프로세스 그룹째** 죽인다. `subprocess.run` 은 직계 자식만 죽여서
  soffice 런처가 띄운 `soffice.bin` 이 고아로 남아 프로필 `.~lock` 을 쥔다 → 같은 워커의
  다음 변환이 영구 실패한다(gunicorn 은 워커를 회수하지 않는다).
"""

from __future__ import annotations

import os
import shutil
import signal
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

#: 변환 1건 타임아웃(초). 최악 동시 점유 = gunicorn 워커수 × 이 값이다(`-w 4` → 4건 × 이 값).
#:
#: 실측(2026-08-13): 현장 `.xls` 239KB(29행×257열) 변환에 **호스트 맥 38s**, 컨테이너
#: amd64 는 콜드 18s/웜 9s. 처음에 60s 로 잡았다가 여유가 1.6배뿐이라 120s 로 올렸다 —
#: **폐쇄망 서버 사양을 모르는 상태에서 타임아웃이 짧으면 정상 문서가 실패한다.**
#: 반대로 무한정 키우면 손상 파일 4건이 워커를 전부 점유한다(그래서 180s 는 안 쓴다).
#: 현장에서 조절할 수 있게 env 로 열어 둔다(탈출구).
#: 테스트는 이 **모듈 속성**을 monkeypatch 해 타임아웃을 유발한다.
_CONVERT_TIMEOUT_SEC = float(os.environ.get("KBP_XLS_CONVERT_TIMEOUT", "120"))


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


def _run_soffice(cmd: List[str], profile_dir: Path, timeout: float) -> subprocess.CompletedProcess:
    """soffice 를 **자기 프로세스 그룹**에서 돌리고 타임아웃 시 그룹째 죽인다.

    `communicate()` 를 쓴다 — `wait()` 로 기다리면 soffice 가 stderr 에 쏟는
    `javaldx` 경고로 파이프가 차서 교착한다.
    """
    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,          # 새 프로세스 그룹 — killpg 대상이 된다
        env={**os.environ, "HOME": str(profile_dir)},  # 전역 ENV 를 바꾸지 않는다
    ) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            out, err = proc.communicate()   # 잔여 출력 회수(파이프 정리)
            raise
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _locate_output(out_dir: Path, stem: str) -> Optional[Path]:
    """산출물 탐색 — 정확한 이름 우선, 없으면 `out_dir` **직하**의 유일한 `*.xlsx`.

    soffice 는 경로를 내부적으로 URL 로 다뤄서 `#`·`%` 가 든 이름이면 산출 basename 이
    입력 stem 과 어긋날 수 있다. 그때 exit=0 인데 파일을 못 찾아 죽는 것을 막는다.
    **비재귀** glob 이다(프로필 트리가 섞이지 않게).
    """
    exact = out_dir / f"{stem}.xlsx"
    if exact.is_file():
        return exact
    candidates = [p for p in out_dir.glob("*.xlsx") if p.is_file()]
    return candidates[0] if len(candidates) == 1 else None


def convert_xls_to_xlsx(path: str | Path, output_dir: Optional[str | Path] = None) -> Path:
    """.xls 파일을 .xlsx 로 변환해 변환된 파일 경로를 반환한다.

    libreoffice --headless 가 없으면 XlsConversionError 를 던진다.

    ``output_dir`` 은 **넘겨라**. 미지정이면 이 함수가 임시 디렉터리를 만드는데 그것을
    지우는 주체가 없어 **문서 1건당 xlsx 사본이 영구 누적**된다(장수 워커 + `/tmp` 공유).
    산출물의 수명은 호출자가 소유해야 한다.
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

    # 변환 1회당 독립 프로필 — gettempdir() **직하**(out_dir 안에 넣지 않는다).
    profile_dir = Path(tempfile.mkdtemp(prefix="lo_"))
    cmd: List[str] = [
        soffice,
        f"-env:UserInstallation=file://{profile_dir}",   # 실행파일 직후 고정
        "--headless",
        "--norestore",
        "--convert-to",
        "xlsx",
        "--outdir",
        str(out_dir),
        str(src),
    ]
    try:
        proc = _run_soffice(cmd, profile_dir, _CONVERT_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as exc:
        raise XlsConversionError(
            f"libreoffice .xls 변환이 {_CONVERT_TIMEOUT_SEC}초 안에 끝나지 않았습니다: {src}"
        ) from exc
    except OSError as exc:
        raise XlsConversionError(f"libreoffice 실행 실패 ({soffice}): {exc}") from exc
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)

    converted = _locate_output(out_dir, src.stem)
    if proc.returncode != 0 or converted is None:
        detail = (proc.stderr or proc.stdout or "").strip()
        listing = ", ".join(sorted(p.name for p in out_dir.iterdir())) or "(비어 있음)"
        raise XlsConversionError(
            f".xls → .xlsx 변환 실패 (exit={proc.returncode}): {src}"
            f"\n출력 디렉터리: {listing}"
            + (f"\nlibreoffice 출력: {detail[:500]}" if detail else "")
        )
    return converted
