"""VL 응답 파싱/정형화 — document-parser pipeline/document_processor.py 이식 (Phase 2c).

원본: /99.projects/jiju_chaekmu/sourceCode/document-parser-backend-src/pipeline/document_processor.py
- parse_vision_language_response_to_elements (16-114)
- normalize_element_content (117-170) + _extract_nested_json_content/_unwrap_json_to_content
- normalize_all_elements (288-299)
치환: core.logging → 표준 logging. 로직은 원본과 동일.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def parse_vision_language_response_to_elements(
    vl_response: str,
    page_number: int,
    start_id: int
) -> Tuple[List[Dict], int]:
    """VL API 응답을 파싱하여 문서 elements 리스트로 변환.

    VL API 응답은 JSON 형식이거나 일반 텍스트일 수 있습니다.
    JSON 파싱에 실패하면 전체를 figure 요소로 처리합니다.

    Returns:
        (elements 리스트, 다음 element ID)
    """
    elements = []
    current_id = start_id

    # JSON 파싱 시도
    try:
        # 마크다운 코드 블록 제거 (```json ... ```)
        cleaned_response = re.sub(r'```json\s*|\s*```', '', vl_response.strip())

        # 이중 따옴표 문제 수정 (""key"": ""value"" -> "key": "value")
        # VL API가 가끔 JSON을 이중 이스케이프하는 경우 처리
        cleaned_response = re.sub(r'""(\w+)"":', r'"\1":', cleaned_response)  # 키 수정
        cleaned_response = re.sub(r':\s*""""', r': ""', cleaned_response)  # 빈 문자열 값 수정

        parsed = json.loads(cleaned_response)

        # Excel enhanced 모드 / JSON 파일: 단일 element 객체인 경우 (category, content 포함)
        if "category" in parsed and "content" in parsed and "elements" not in parsed:
            # 이미 완전한 element 구조 - ID와 page만 재설정
            element = {
                "category": parsed["category"],
                "content": {
                    "html": parsed["content"].get("html", ""),
                    "markdown": parsed["content"].get("markdown", ""),
                    "text": parsed["content"].get("text", "")
                },
                "coordinates": parsed.get("coordinates", []),
                "id": current_id,
                "page": page_number
            }
            elements.append(element)
            current_id += 1
            return elements, current_id

        # elements 배열 확인 (일반 VL API 응답)
        if "elements" in parsed and isinstance(parsed["elements"], list):
            for elem_data in parsed["elements"]:
                # 기본 구조 검증
                if "category" not in elem_data or "content" not in elem_data:
                    logger.warning(f"Invalid element structure: {elem_data}")
                    continue

                # element 생성
                element = {
                    "category": elem_data["category"],
                    "content": {
                        "html": elem_data["content"].get("html", ""),
                        "markdown": elem_data["content"].get("markdown", ""),
                        "text": elem_data["content"].get("text", "")
                    },
                    "coordinates": elem_data.get("coordinates", []),
                    "id": current_id,
                    "page": page_number
                }
                elements.append(element)
                current_id += 1
        else:
            raise ValueError("No 'elements' array found in response")

    except (json.JSONDecodeError, ValueError) as e:
        # JSON 파싱 실패 시 전체를 figure로 처리
        logger.warning(f"Failed to parse VL response as JSON: {e}. Treating as plain text figure.")
        elements.append({
            "category": "figure",
            "content": {
                "html": "",
                "markdown": vl_response,
                "text": ""
            },
            "coordinates": [],
            "id": current_id,
            "page": page_number
        })
        current_id += 1

    return elements, current_id


def normalize_element_content(element: Dict) -> Dict:
    """단일 element의 content 필드를 정형화.

    content 필드(html, markdown, text)가 실제 콘텐츠 문자열만 포함하도록 보장합니다.
    중첩된 JSON 구조가 들어온 경우 실제 콘텐츠를 추출합니다.

    특수 케이스: category="json"의 text 필드는 JSON이 정상이므로 스킵합니다.
    """
    content = element.get("content", {})
    category = element.get("category", "")

    # content가 dict가 아니면 래핑
    if not isinstance(content, dict):
        element["content"] = {"html": "", "markdown": str(content), "text": ""}
        return element

    # 각 필드 정형화
    for field in ["html", "markdown", "text"]:
        value = content.get(field, "")

        # str이 아니면 직렬화 (dict/list가 직접 들어온 경우)
        if not isinstance(value, str):
            content[field] = json.dumps(value, ensure_ascii=False) if value else ""
            continue

        # category=json의 text 필드는 JSON이 정상 → 스킵
        if category == "json" and field == "text":
            continue

        # 중첩 JSON 탐지 및 실제 콘텐츠 추출
        # VL 8B 모델이 content를 이중 직렬화하는 경우 대비: 2-pass
        cleaned = _extract_nested_json_content(value, field, category)
        if cleaned is not None:
            # 1차 추출 결과가 여전히 JSON 구조이면 2차 추출 시도
            s = cleaned.lstrip()
            if s and s[0] in ('{', '['):
                second = _extract_nested_json_content(cleaned, field, category)
                if second is not None:
                    cleaned = second
            content[field] = cleaned

    # 누락 필드 기본값 보장
    for field in ["html", "markdown", "text"]:
        if field not in content:
            content[field] = ""

    element["content"] = content
    return element


def _extract_nested_json_content(value: str, field: str, category: str) -> Optional[str]:
    """content 필드에서 중첩 JSON을 탐지하고 실제 콘텐츠를 추출.

    8B VL 모델이 guided JSON 환경에서 content 필드에 JSON 구조를
    문자열로 넣는 경우를 처리합니다.

    탐지 패턴:
    1. 값이 {/[로 시작 (직접 JSON)
    2. ```json ... ``` 코드블록으로 감싸진 JSON
    """
    stripped = value.strip()
    if not stripped:
        return None

    json_str = None

    # 패턴 1: {/[로 시작하는 직접 JSON
    if stripped.startswith('{') or stripped.startswith('['):
        json_str = stripped

    # 패턴 2: ```json ... ``` 코드블록 감싸기
    if json_str is None:
        match = re.match(
            r'^```(?:json)?\s*\n?(.*?)\n?\s*```\s*$',
            stripped,
            re.DOTALL
        )
        if match:
            candidate = match.group(1).strip()
            if candidate.startswith('{') or candidate.startswith('['):
                json_str = candidate

    if json_str is None:
        return None

    # JSON 파싱 시도
    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed, (dict, list)):
        return None

    return _unwrap_json_to_content(parsed, field, category)


def _unwrap_json_to_content(parsed: Any, field: str, category: str) -> Optional[str]:
    """파싱된 JSON에서 실제 콘텐츠를 추출.

    8B VL 모델이 생성하는 주요 중첩 패턴:
    - {"elements": [{"content": {"html": "...", "markdown": "..."}}]}
    - {"category": ..., "content": {"html": "..."}}
    - {"html": "...", "markdown": "..."} (content 객체 자체)
    """
    if isinstance(parsed, dict):
        # 패턴: {"elements": [...]} 중첩 응답
        if "elements" in parsed and isinstance(parsed["elements"], list):
            parts = []
            for elem in parsed["elements"]:
                if isinstance(elem, dict) and "content" in elem:
                    elem_content = elem["content"]
                    if isinstance(elem_content, dict):
                        text = (
                            elem_content.get(field, "")
                            or elem_content.get("markdown", "")
                            or elem_content.get("html", "")
                            or elem_content.get("text", "")
                        )
                        if text:
                            parts.append(text)
            return "\n\n".join(parts) if parts else ""

        # 패턴: 단일 element {"category": ..., "content": {...}}
        if "content" in parsed and isinstance(parsed["content"], dict):
            return parsed["content"].get(field, "")

        # 패턴: content 객체 자체 {"html": "...", "markdown": "..."}
        if field in parsed and isinstance(parsed[field], str):
            return parsed[field]

        # 알 수 없는 JSON → 직렬화하여 콘텐츠 유실 방지
        return json.dumps(parsed, ensure_ascii=False)

    if isinstance(parsed, list):
        # element 배열
        parts = []
        for item in parsed:
            if isinstance(item, dict) and "content" in item:
                item_content = item["content"]
                if isinstance(item_content, dict):
                    text = item_content.get(field, "")
                    if text:
                        parts.append(text)
        return "\n\n".join(parts) if parts else json.dumps(parsed, ensure_ascii=False)

    return None


def normalize_all_elements(elements: List[Dict]) -> List[Dict]:
    """element 리스트 전체의 content 필드를 정형화 (in-place 수정)."""
    for element in elements:
        normalize_element_content(element)
    return elements
