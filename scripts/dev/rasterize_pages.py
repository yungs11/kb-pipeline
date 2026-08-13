#!/usr/bin/env python
"""개발 전용 — PDF 페이지를 **이미지-only** PDF 로 재래스터화한다(제품 코드 아님).

Plan A §V4 용. 네이티브 텍스트 PDF 는 triage 의 `has_native_text`(char_count > 20) 때문에
절대 `OCR_NEEDED` 가 되지 않아 스캔 레인(paddle_gw)에 들어가지 못한다. 텍스트 레이어를 없앤
이미지-only PDF 로 바꿔야 실제 스캔 문서로 취급된다.

    python scripts/dev/rasterize_pages.py IN.pdf OUT.pdf 17 33
"""
import sys

import pymupdf


def rasterize(src_path: str, dst_path: str, pages: list[int], dpi: int = 300) -> None:
    src = pymupdf.open(src_path)
    out = pymupdf.open()
    for pno in pages:
        pix = src[pno - 1].get_pixmap(dpi=dpi)
        page = out.new_page(width=pix.width * 72 / dpi, height=pix.height * 72 / dpi)
        page.insert_image(page.rect, pixmap=pix)
    out.save(dst_path)
    print(f"{dst_path}: {len(pages)} page(s) from {src_path} @ {dpi}dpi")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    rasterize(sys.argv[1], sys.argv[2], [int(a) for a in sys.argv[3:]])
