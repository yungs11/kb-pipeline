"""이미지 처리 유틸 — document-parser utils/image.py + handlers/image_handler.py 이식 (Phase 2c).

이식 함수: image_to_base64 / multipage_image_to_base64(+stream) / get_image_page_count /
compress_image_bytes / image_bytes_to_base64 / serialize_image_to_bytes(내부 의존) +
image_file_to_base64_list(handlers/image_handler.py:31-56, parse_page_range 포함).
치환(plan 이식 규칙 1): get_config_value/get_config → env 직독 + 원본 config 기본값 상수.
- IMAGE_CONVERSION_DPI(기본 450)
"""

import base64
import io
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageOps

LANCZOS = Image.LANCZOS if hasattr(Image, "LANCZOS") else Image.BICUBIC

# 원본 core/config/settings.py ImageConfig 기본값
_PNG_COMPRESS_LEVEL = 3
_PNG_OPTIMIZE = False
_JPEG_QUALITY = 85
_JPEG_OPTIMIZE = True
_WEBP_QUALITY = 90
_WEBP_METHOD = 6
_OUTPUT_FORMAT = "AUTO"

IMAGE_ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"}


def _conversion_dpi() -> int:
    return int(os.environ.get("IMAGE_CONVERSION_DPI", "450"))


def _build_dpi_tuple(dpi: Optional[int]) -> Tuple[int, int]:
    """Pillow에서 사용하는 DPI 튜플을 생성합니다."""
    target = dpi or _conversion_dpi()
    return target, target


def auto_correct_image_metadata(image: Image.Image) -> Image.Image:
    """EXIF 메타데이터를 활용해 이미지 방향을 보정합니다."""
    try:
        return ImageOps.exif_transpose(image)
    except Exception:
        # EXIF 데이터가 없거나 처리 중 오류가 발생해도 원본 이미지를 반환한다.
        return image


def serialize_image_to_bytes(
    image: Image.Image,
    format_hint: Optional[str] = None,
    dpi: Optional[int] = None,
    extra_save_kwargs: Optional[Dict[str, Any]] = None,
) -> bytes:
    """공통 설정을 적용해 Pillow 이미지를 바이트로 직렬화합니다."""
    normalized_hint = format_hint.upper() if format_hint else None
    config_format = _OUTPUT_FORMAT.upper()

    if normalized_hint:
        target_format = normalized_hint
    elif config_format in ("", "AUTO", "ORIGINAL"):
        target_format = (image.format or "PNG").upper()
    else:
        target_format = config_format

    if target_format == "JPG":
        target_format = "JPEG"

    save_kwargs: Dict[str, Any] = {}
    if extra_save_kwargs:
        # 필수 인자(예: format)가 덮어쓰이지 않도록 안전하게 병합한다.
        save_kwargs.update({k: v for k, v in extra_save_kwargs.items() if k != "format"})

    save_kwargs.setdefault("dpi", _build_dpi_tuple(dpi))

    # 포맷별 압축 전략 적용
    if target_format == "PNG":
        save_kwargs.setdefault("compress_level", _PNG_COMPRESS_LEVEL)
        save_kwargs.setdefault("optimize", _PNG_OPTIMIZE)
    elif target_format == "JPEG":
        # JPEG는 RGB 모드 필요 (RGBA, P, L 등 변환)
        if image.mode in ("RGBA", "LA", "P"):
            # 투명도가 있는 경우 흰색 배경으로 변환
            rgb_image = Image.new("RGB", image.size, (255, 255, 255))
            if image.mode == "P":
                image = image.convert("RGBA")
            rgb_image.paste(image, mask=image.split()[-1] if image.mode in ("RGBA", "LA") else None)
            image = rgb_image
        elif image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        save_kwargs.setdefault("quality", _JPEG_QUALITY)
        save_kwargs.setdefault("optimize", _JPEG_OPTIMIZE)
    elif target_format == "WEBP":
        save_kwargs.setdefault("quality", _WEBP_QUALITY)
        save_kwargs.setdefault("method", _WEBP_METHOD)

    buffer = io.BytesIO()
    image.save(buffer, format=target_format, **save_kwargs)
    return buffer.getvalue()


def _resize_with_aspect_ratio(
    image: Image.Image,
    max_width: Optional[int] = None,
    max_height: Optional[int] = None,
) -> Image.Image:
    """비율을 유지하며 최대 너비/높이에 맞춰 리사이즈합니다."""
    if not max_width and not max_height:
        return image

    width, height = image.size
    target_ratios = []

    if max_width and max_width > 0 and width > max_width:
        target_ratios.append(max_width / width)
    if max_height and max_height > 0 and height > max_height:
        target_ratios.append(max_height / height)

    if not target_ratios:
        return image

    scale = min(target_ratios)
    if scale >= 1:
        return image

    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    return image.resize(new_size, LANCZOS)


def compress_image_bytes(
    image_bytes: bytes,
    target_format: Optional[str] = None,
    *,
    max_width: Optional[int] = None,
    max_height: Optional[int] = None,
    quality: Optional[int] = None,
    dpi: Optional[int] = None,
    extra_save_kwargs: Optional[Dict[str, Any]] = None,
    grayscale: bool = False,
) -> Tuple[bytes, str, Tuple[int, int]]:
    """이미지 바이트를 리사이즈 및 압축하여 다시 직렬화합니다.

    Returns:
        압축된 이미지 바이트, 사용된 포맷(대문자), 최종 이미지 크기 튜플
    """
    with Image.open(io.BytesIO(image_bytes)) as original_image:
        corrected = auto_correct_image_metadata(original_image)
        if corrected is original_image:
            working_image = original_image.copy()
        else:
            working_image = corrected.copy()
            corrected.close()

    try:
        # 비율 유지 리사이징
        resized_image = _resize_with_aspect_ratio(
            working_image,
            max_width=max_width,
            max_height=max_height,
        )

        # Grayscale 변환
        if grayscale and resized_image.mode != 'L':
            resized_image_gray = resized_image.convert('L')
            resized_image.close()
            resized_image = resized_image_gray

        save_kwargs: Dict[str, Any] = {}
        if extra_save_kwargs:
            save_kwargs.update(extra_save_kwargs)
        if quality is not None:
            save_kwargs["quality"] = quality

        normalized_target = (target_format or "").upper()
        if normalized_target in ("", "AUTO"):
            normalized_target = (working_image.format or "PNG").upper()

        try:
            processed_bytes = serialize_image_to_bytes(
                resized_image,
                format_hint=normalized_target,
                dpi=dpi,
                extra_save_kwargs=save_kwargs or None,
            )
            final_size = resized_image.size
        finally:
            resized_image.close()

        return processed_bytes, normalized_target, final_size
    finally:
        working_image.close()


def image_bytes_to_base64(image_bytes: bytes) -> str:
    """이미지 바이트 데이터를 base64 문자열로 변환합니다."""
    if image_bytes is None:
        raise ValueError("이미지 바이트 데이터가 제공되지 않았습니다.")

    try:
        return base64.b64encode(image_bytes).decode('utf-8')
    except Exception as e:
        raise ValueError(f"이미지 바이트 인코딩 중 오류 발생: {str(e)}") from e


def image_to_base64(image_path: str) -> str:
    """이미지 파일을 base64 문자열로 변환합니다."""
    try:
        with Image.open(image_path) as image:
            original_format = image.format
            corrected_image = auto_correct_image_metadata(image)
            try:
                image_bytes = serialize_image_to_bytes(
                    corrected_image,
                    format_hint=original_format,
                )
            finally:
                if corrected_image is not image:
                    corrected_image.close()
            return image_bytes_to_base64(image_bytes)
    except Exception as e:
        raise Exception(f"단일 이미지 변환 중 오류 발생: {str(e)}")


def multipage_image_to_base64_stream(file_path, max_pages=None, page_numbers=None):
    """멀티페이지 이미지를 base64 문자열로 스트리밍합니다."""
    with Image.open(file_path) as img:
        total_frames = getattr(img, 'n_frames', 1)
        original_format = img.format

        # 페이지 범위 결정
        if page_numbers:
            # 0-based 인덱스로 변환 및 유효성 검사
            frames_to_process = [p - 1 for p in page_numbers if 0 < p <= total_frames]
        elif max_pages:
            frames_to_process = list(range(min(total_frames, max_pages)))
        else:
            frames_to_process = list(range(total_frames))

        for i in frames_to_process:
            img.seek(i)
            frame = img.copy()
            corrected_frame = auto_correct_image_metadata(frame)
            try:
                image_bytes = serialize_image_to_bytes(
                    corrected_frame,
                    format_hint=original_format or frame.format,
                )
                yield image_bytes_to_base64(image_bytes)
            finally:
                if corrected_frame is not frame:
                    corrected_frame.close()
                frame.close()


def multipage_image_to_base64(file_path, max_pages=None, page_numbers=None):
    """멀티페이지 이미지를 base64 문자열 리스트로 변환합니다."""
    return list(multipage_image_to_base64_stream(file_path, max_pages, page_numbers))


def get_image_page_count(image_path: str) -> int:
    """이미지의 전체 페이지 수를 반환합니다 (멀티페이지가 아니면 1)."""
    try:
        with Image.open(image_path) as img:
            if hasattr(img, 'n_frames'):
                return img.n_frames
            else:
                return 1
    except Exception as e:
        raise Exception(f"이미지 페이지 수 확인 중 오류 발생: {str(e)}")


def parse_page_range(page_range: Optional[str]) -> Optional[List[int]]:
    """페이지 범위 문자열('1-5', '1,3,5', '1-3,5')을 1-based 번호 리스트로 파싱.

    원본 utils/page_range.py:parse_page_range 계약 유지(간소 이식). None/빈 문자열 → None.
    """
    if not page_range:
        return None
    page_numbers: set[int] = set()
    for part in page_range.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, _, end_s = part.partition("-")
            start, end = int(start_s), int(end_s)
            if start < 1 or end < start:
                raise ValueError(f"잘못된 페이지 범위: {part!r}")
            page_numbers.update(range(start, end + 1))
        else:
            n = int(part)
            if n < 1:
                raise ValueError(f"잘못된 페이지 번호: {part!r}")
            page_numbers.add(n)
    return sorted(page_numbers) if page_numbers else None


def image_file_to_base64_list(file_path: str, page_range: Optional[str] = None) -> List[str]:
    """이미지 파일을 base64 문자열 리스트로 변환합니다.

    단일 페이지 이미지와 멀티페이지 이미지(TIFF 등)를 모두 처리합니다.
    (원본 pipeline/handlers/image_handler.py:image_file_to_base64_list)
    """
    page_count = get_image_page_count(file_path)

    if page_count > 1:
        # 멀티페이지 이미지 (TIFF 등)
        page_numbers = parse_page_range(page_range) if page_range else None
        return multipage_image_to_base64(file_path, page_numbers=page_numbers)

    # 단일 페이지 이미지
    if page_range:
        page_numbers = parse_page_range(page_range)
        if page_numbers and 1 not in page_numbers:
            return []

    return [image_to_base64(file_path)]


def is_supported_image(file_path: str) -> bool:
    """지원 가능한 이미지 파일인지 확인합니다."""
    return Path(file_path).suffix.lower() in IMAGE_ALLOWED_EXTENSIONS
