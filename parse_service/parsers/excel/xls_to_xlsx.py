"""레거시 `.xls`(BIFF) 바이트 → `.xlsx` 바이트 어댑터.

`csv_to_xlsx.py` 와 같은 층위다 — **엑셀 레인 입구**에서 바이트를 갈아끼워, 하류
(백엔드 3종·게이트·청킹)가 `.xlsx` 만 보게 한다. 그래서 하류 코드 변경이 0 이고,
전결(Tier1)·계층(Tier1.5) 라우팅의 확장자 게이트도 자연히 통과한다.

**왜 입구인가** — `kordoc_backend` 의 동반 openpyxl 읽기 한 곳만 고치면 죽는 건 멈춰도
전결 `.xls` 가 kordoc 으로 새는 조용한 품질 손실이 남고, 게이트(`excel_gate`)에서 같은
결함을 또 고쳐야 한다.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

# ★ 모듈 최상위 import — 함수 내부 지연 import 로 두면 테스트가
#   `setattr(xls_to_xlsx, "convert_xls_to_xlsx", fake)` 로 패치할 지점이 없어진다.
from parse_service.parsers.excel.excel_parser_rag.loaders.xls_converter import convert_xls_to_xlsx


def xls_bytes_to_xlsx(file_bytes: bytes, _filename: str | None = None) -> bytes:
    """`.xls` 바이트를 `.xlsx` 바이트로 변환한다. 실패 시 ``XlsConversionError``.

    ``_filename`` 은 받아만 두고 **쓰지 않는다**(호출부의 csv 관례와 시그니처를 맞추기
    위한 것). 디스크 이름은 아래처럼 **고정**한다 — 사용자 파일명을 쓰면 `#`·`%` 가 든
    이름에서 soffice 의 산출 basename 이 어긋나 exit=0 인데 산출물을 못 찾는 실패가
    **현장에서만** 난다(픽스처는 전부 ASCII 라 테스트로 안 잡힌다).
    `document_title` 은 호출부가 원본 stem 에서 따로 잡으므로 여기서 이름을 보존할 이유도 없다.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="xls2xlsx_"))
    try:
        src = tmpdir / "src.xls"
        src.write_bytes(file_bytes)
        out = convert_xls_to_xlsx(src, output_dir=tmpdir)   # 입력·출력 같은 tmpdir
        return out.read_bytes()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
