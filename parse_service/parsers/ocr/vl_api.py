"""Vision-Language 모델 API 호출 — document-parser model/vision_language_model.py 이식 (Phase 2c).

원본의 call_vl_api_with_base64 경로만 이식(multimodal/batch 제외).
치환(plan 이식 규칙 1): core.config/get_config_value → env 직독
- MODEL_API_URL / MODEL_API_KEY / MODEL_NAME(기본 qwen/qwen3-vl-235b-a22b-instruct)
- VL_MAX_TOKENS(기본 2000) / USE_GUIDED_JSON(기본 "1") / GUIDED_JSON_MODE(기본 extra_body)
- VL_MODEL_TIMEOUT(기본 600)

HTTP 연결 안정성(원본 동일):
- timeout 세분화 (connect/read/write/pool 분리)
- keepalive race condition 대응 (ReadError 자동 1회 재시도)
- 5xx/429 상태코드 자동 1회 재시도
"""

import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

import httpx

# LLM 전송 포맷: parse-svc 렌더 경로(JPEG)에 맞춤
DEFAULT_IMAGE_MIME = "image/jpeg"
logger = logging.getLogger(__name__)

# HTTP 레벨 자동 재시도 설정
_HTTP_RETRY_COUNT = 1           # 일시적 오류 시 최대 재시도 횟수
_HTTP_RETRY_DELAY = 1.0         # 재시도 전 대기 (초)
_RETRYABLE_STATUS_CODES = {429, 502, 503, 504}  # 재시도 대상 상태코드

_DEFAULT_MODEL_NAME = "qwen/qwen3-vl-235b-a22b-instruct"

# =============================================================================
# Guided JSON Schema — OCR 응답 구조 (인퍼런스 엔진 레벨 강제용)
# =============================================================================

OCR_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "elements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["table", "figure"],
                    },
                    "content": {
                        "type": "object",
                        "properties": {
                            "html": {"type": "string"},
                            "markdown": {"type": "string"},
                            "text": {"type": "string"},
                        },
                        "required": ["html", "markdown", "text"],
                    },
                    "coordinates": {"type": "array"},
                    "id": {"type": "integer"},
                    "page": {"type": "integer"},
                },
                "required": ["category", "content", "id", "page"],
            },
        }
    },
    "required": ["elements"],
}

# 글로벌 비동기 HTTP 클라이언트 (재사용)
_http_client: Optional[httpx.AsyncClient] = None
_http_client_loop: Optional[asyncio.AbstractEventLoop] = None


def get_http_client() -> httpx.AsyncClient:
    """비동기 HTTP 클라이언트 싱글톤을 반환합니다.

    timeout 세분화:
    - connect: TCP 핸드셰이크 (60초)
    - read: 서버 응답 대기 — VL 모델 추론 시간 (VL_MODEL_TIMEOUT)
    - write: 요청 전송 — base64 이미지 업로드 (120초)
    - pool: 커넥션 풀 획득 대기 (30초)

    원본과 달리 parse-svc 는 호출마다 ``asyncio.run``(ocr_elements_sync) 으로 새 이벤트루프를
    만들 수 있다 — AsyncClient 는 생성된 루프에 묶이므로, 루프가 바뀌었으면 재생성한다
    (죽은 루프의 커넥션 재사용 시 "Event loop is closed" — Phase 2c 스택 검증에서 발견).
    """
    global _http_client, _http_client_loop
    loop = asyncio.get_running_loop()
    if _http_client is None or _http_client.is_closed or _http_client_loop is not loop:
        _http_client_loop = loop
        vl_timeout = float(os.environ.get("VL_MODEL_TIMEOUT", "600"))
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                timeout=vl_timeout,     # 전체 폴백
                connect=60.0,           # TCP 핸드셰이크
                read=vl_timeout,        # VL 추론 응답 대기
                write=120.0,            # base64 이미지 전송
                pool=30.0,              # 커넥션 풀 획득
            ),
            limits=httpx.Limits(
                max_keepalive_connections=20,
                max_connections=100,
                keepalive_expiry=120,   # keepalive 유지 시간 (초) — 서버 idle timeout보다 짧게
            ),
        )
        logger.info(
            f"HTTP client initialized: timeout(connect=60s, read={vl_timeout}s, "
            f"write=120s, pool=30s), keepalive_expiry=120s"
        )
    return _http_client


async def close_http_client():
    """HTTP 클라이언트를 종료합니다 (앱 종료 시 호출)."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None
        logger.info("HTTP client closed")


async def call_vl_api_with_base64(
    base64_image: str,
    user_prompt: str,
    system_prompt: str
) -> Tuple[str, float]:
    """base64 이미지로 Vision-Language 모델 API를 호출합니다.

    Returns:
        (모델 응답 문자열, 호출 소요 시간(초)) 튜플
    """
    payload = _build_payload(base64_image, user_prompt, system_prompt)
    response_json, elapsed_time = await _request_vl_api(payload)
    return _extract_result(response_json), elapsed_time


def _apply_guided_json(payload: Dict[str, Any]) -> Dict[str, Any]:
    """설정(env)에 따라 guided_json 스키마를 payload에 주입한다."""
    use_guided = os.environ.get("USE_GUIDED_JSON", "1").lower() not in ("0", "false", "")
    if not use_guided:
        return payload

    mode = os.environ.get("GUIDED_JSON_MODE") or "extra_body"
    if mode == "response_format":
        # OpenRouter / OpenAI 호환 방식
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "ocr_output",
                "schema": OCR_JSON_SCHEMA,
            },
        }
    else:
        # vLLM extra_body 방식 (기본)
        payload["extra_body"] = {
            "structured_outputs": {"json": OCR_JSON_SCHEMA}
        }

    logger.debug(f"Guided JSON applied (mode={mode})")
    return payload


def _build_payload(base64_image: str, user_prompt: str, system_prompt: str) -> Dict[str, Any]:
    """Vision-Language 모델 API 호출 페이로드를 구성합니다."""
    model_name = os.environ.get("MODEL_NAME", _DEFAULT_MODEL_NAME)
    max_tokens = os.environ.get("VL_MAX_TOKENS", "2000")

    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{DEFAULT_IMAGE_MIME};base64,{base64_image}"
                        },
                    },
                ],
            },
        ],
        "max_tokens": int(max_tokens),
        "temperature": 0.1
    }
    # OpenRouter reasoning off — qwen3.5 등 reasoning 모델이 OCR/서술에 수천 토큰 think 를
    # 생성해 느려지는 것 방지(KBP_VL_DISABLE_REASONING, 기본 on). edgequake 와 동일 정책.
    if os.environ.get("KBP_VL_DISABLE_REASONING", "1").lower() not in ("0", "false", "off", ""):
        payload["reasoning"] = {"enabled": False}
    return _apply_guided_json(payload)


def _extract_result(response_json: Dict[str, Any]) -> str:
    """Vision-Language 모델 API 응답에서 결과 텍스트를 추출한다."""
    return response_json.get("choices", [{}])[0].get("message", {}).get("content", "")


def _classify_error(exc: Exception) -> str:
    """httpx 예외를 운영 진단용 카테고리로 분류한다."""
    if isinstance(exc, httpx.ConnectTimeout):
        return "CONNECT_TIMEOUT"
    elif isinstance(exc, httpx.ReadTimeout):
        return "READ_TIMEOUT"
    elif isinstance(exc, httpx.WriteTimeout):
        return "WRITE_TIMEOUT"
    elif isinstance(exc, httpx.PoolTimeout):
        return "POOL_TIMEOUT"
    elif isinstance(exc, httpx.ConnectError):
        return "CONNECT_ERROR"
    elif isinstance(exc, httpx.ReadError):
        return "READ_ERROR"
    elif isinstance(exc, httpx.WriteError):
        return "WRITE_ERROR"
    elif isinstance(exc, httpx.TimeoutException):
        return "TIMEOUT"
    elif isinstance(exc, httpx.RequestError):
        return "REQUEST_ERROR"
    return type(exc).__name__


def _is_retryable_error(exc: Exception) -> bool:
    """HTTP 레벨에서 즉시 재시도할 수 있는 일시적 오류인지 판단한다.

    ReadError: keepalive race condition (서버가 idle 연결을 먼저 닫음)
    ConnectError: 일시적 네트워크 불안정
    PoolTimeout: 커넥션 풀 일시 포화
    """
    return isinstance(exc, (httpx.ReadError, httpx.ConnectError, httpx.PoolTimeout))


async def _request_vl_api(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], float]:
    """Vision-Language 모델 API를 호출하고 응답 JSON과 소요 시간을 반환한다.

    일시적 오류(ReadError, ConnectError, 5xx, 429)에 대해 1회 자동 재시도합니다.

    Raises:
        httpx.RequestError: 네트워크 요청이 실패한 경우 (재시도 후에도 실패)
        httpx.HTTPStatusError: HTTP 상태 코드가 200이 아닌 경우 (재시도 후에도 실패)
    """
    model_api_url = os.environ.get("MODEL_API_URL", "")
    model_api_key = os.environ.get("MODEL_API_KEY", "")
    model_name = os.environ.get("MODEL_NAME", _DEFAULT_MODEL_NAME)

    headers = {
        "Authorization": f"Bearer {model_api_key}",
        "Content-Type": "application/json",
    }

    # payload 크기 (로그용, 대략적)
    payload_size_kb = len(str(payload)) // 1024

    client = get_http_client()
    last_exc = None

    for attempt in range(_HTTP_RETRY_COUNT + 1):
        start_time = time.time()

        try:
            response = await client.post(
                url=model_api_url,
                headers=headers,
                json=payload,
            )
        except httpx.RequestError as exc:
            elapsed = time.time() - start_time
            error_type = _classify_error(exc)

            if attempt < _HTTP_RETRY_COUNT and _is_retryable_error(exc):
                logger.warning(
                    f"VL API {error_type} (attempt {attempt + 1}/{_HTTP_RETRY_COUNT + 1}): "
                    f"model={model_name}, url={model_api_url}, "
                    f"payload_size={payload_size_kb}KB, elapsed={elapsed:.1f}s — "
                    f"retrying in {_HTTP_RETRY_DELAY}s"
                )
                await asyncio.sleep(_HTTP_RETRY_DELAY)
                last_exc = exc
                continue

            # 최종 실패
            logger.error(
                f"VL API {error_type}: model={model_name}, url={model_api_url}, "
                f"payload_size={payload_size_kb}KB, elapsed={elapsed:.1f}s, "
                f"attempts={attempt + 1} — {exc}"
            )
            raise

        elapsed_time = time.time() - start_time

        # 재시도 대상 상태코드 처리 (429, 502, 503, 504)
        if response.status_code in _RETRYABLE_STATUS_CODES:
            if attempt < _HTTP_RETRY_COUNT:
                # 429면 Retry-After 헤더 존재할 수 있음
                retry_after = response.headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else _HTTP_RETRY_DELAY

                logger.warning(
                    f"VL API HTTP {response.status_code} (attempt {attempt + 1}/{_HTTP_RETRY_COUNT + 1}): "
                    f"model={model_name}, elapsed={elapsed_time:.1f}s — "
                    f"retrying in {delay}s"
                )
                await asyncio.sleep(delay)
                continue

            # 재시도 후에도 실패
            logger.error(
                f"VL API HTTP {response.status_code}: model={model_name}, "
                f"url={model_api_url}, elapsed={elapsed_time:.1f}s, "
                f"attempts={attempt + 1}, response={response.text[:500]}"
            )
            response.raise_for_status()

        # 기타 비-200 상태코드
        if response.status_code != 200:
            logger.error(
                f"VL API HTTP {response.status_code}: model={model_name}, "
                f"url={model_api_url}, elapsed={elapsed_time:.1f}s, "
                f"response={response.text[:500]}"
            )
            response.raise_for_status()

        # 재시도 성공 시 로그
        if attempt > 0:
            logger.info(
                f"VL API retry succeeded (attempt {attempt + 1}): "
                f"model={model_name}, elapsed={elapsed_time:.1f}s"
            )

        # 성공 — JSON 파싱
        try:
            response_json = response.json()
            logger.debug(
                f"VL API OK: model={model_name}, elapsed={elapsed_time:.1f}s, "
                f"payload_size={payload_size_kb}KB"
            )
            return response_json, elapsed_time
        except ValueError as e:
            logger.error(
                f"VL API JSON parse error: model={model_name}, "
                f"status={response.status_code}, elapsed={elapsed_time:.1f}s, "
                f"error={e}"
            )
            _sensitive_headers = {"authorization", "set-cookie", "x-api-key", "proxy-authorization"}
            _safe_headers = {
                k: ("***" if k.lower() in _sensitive_headers else v)
                for k, v in response.headers.items()
            }
            logger.error(f"VL API response headers: {_safe_headers}")
            logger.error(f"VL API response body (first 2000 chars): {response.text[:2000]}")

            # 빈 응답 반환 (처리 계속 진행)
            logger.warning("Returning error placeholder to continue processing")
            return {
                "choices": [{
                    "message": {
                        "content": f"[Error: Failed to parse API response - {str(e)}]"
                    }
                }]
            }, elapsed_time

    # 여기까지 오면 모든 시도 소진 (이론적으로 도달 불가)
    raise last_exc or RuntimeError("VL API request failed after all retries")


__all__ = [
    "call_vl_api_with_base64",
    "get_http_client",
    "close_http_client",
    "OCR_JSON_SCHEMA",
]
