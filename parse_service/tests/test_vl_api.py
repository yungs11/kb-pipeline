"""MODEL_NAME configuration contract — 암묵 기본값 금지.

2026-08 실사고: 호스트 dev 에 `MODEL_NAME` 이 없어 코드 기본값(qwen3-vl-235b)으로 조용히
떨어졌고 compose 는 122b 라 **측정 전체가 다른 모델로 돌았다**. 근본 원인은 오타가 아니라
`.env.example`·`scripts/parse-svc.env.example` 에 선언 자체가 없던 **configuration
contract 부재**였다.

Acceptance: **미설정 상태에서 어떤 모델도 암묵적으로 실행되지 않는다.**
"""
import pytest

from parse_service.parsers.ocr import vl_api


def test_model_name_required(monkeypatch):
    monkeypatch.delenv("MODEL_NAME", raising=False)
    with pytest.raises(RuntimeError, match="MODEL_NAME"):
        vl_api._require_model_name()


def test_model_name_blank_is_unset(monkeypatch):
    """빈 문자열도 미설정으로 본다(compose 가 빈 값을 넘길 수 있다)."""
    monkeypatch.setenv("MODEL_NAME", "   ")
    with pytest.raises(RuntimeError, match="MODEL_NAME"):
        vl_api._require_model_name()


def test_payload_uses_env_model(monkeypatch):
    monkeypatch.setenv("MODEL_NAME", "qwen/qwen3.5-122b-a10b")
    payload = vl_api._build_payload("Zm9v", "user", "system")
    assert payload["model"] == "qwen/qwen3.5-122b-a10b"


def test_payload_raises_without_model(monkeypatch):
    """빌드 경로에서도 암묵 실행이 없다 — 페이로드가 만들어지지 않는다."""
    monkeypatch.delenv("MODEL_NAME", raising=False)
    with pytest.raises(RuntimeError, match="MODEL_NAME"):
        vl_api._build_payload("Zm9v", "user", "system")


def test_no_implicit_default_symbol():
    """`_DEFAULT_MODEL_NAME` 이 되살아나면 사고가 재발한다."""
    assert not hasattr(vl_api, "_DEFAULT_MODEL_NAME")


# ── 2026-08-11 Phase 1: max_tokens 배선 + 프로바이더 차단 ────────────────────

import pytest


def test_max_tokens_priority(monkeypatch):
    """인자 > VL_MAX_TOKENS env > 2000."""
    monkeypatch.setenv("MODEL_NAME", "m")
    monkeypatch.delenv("VL_MAX_TOKENS", raising=False)
    assert vl_api._build_payload("x", "u", "s")["max_tokens"] == 2000
    monkeypatch.setenv("VL_MAX_TOKENS", "3000")
    assert vl_api._build_payload("x", "u", "s")["max_tokens"] == 3000
    assert vl_api._build_payload("x", "u", "s", max_tokens=8000)["max_tokens"] == 8000


def test_max_tokens_is_keyword_only():
    """positional 4번째 인자는 **즉시 TypeError** — 상류 `except Exception` 에 삼켜져
    조용히 빈 페이지가 되는 경로를 문법으로 막는다(규약이 아니라)."""
    with pytest.raises(TypeError):
        vl_api._build_payload("x", "u", "s", 8000)      # type: ignore[misc]


def test_call_api_max_tokens_keyword_only():
    import inspect
    sig = inspect.signature(vl_api.call_vl_api_with_base64)
    assert sig.parameters["max_tokens"].kind is inspect.Parameter.KEYWORD_ONLY


def test_ocr_entry_passes_max_tokens_as_keyword():
    """`ocr_file_to_elements`/`ocr_elements_sync` 도 keyword-only 여야 한다 —
    positional 드리프트가 `except Exception` 에 삼켜지면 전 페이지가 빈 결과가 된다."""
    import inspect
    from parse_service.parsers import ocr
    for fn in (ocr.ocr_file_to_elements, ocr.ocr_elements_sync):
        p = inspect.signature(fn).parameters["max_tokens"]
        assert p.kind is inspect.Parameter.KEYWORD_ONLY, fn.__name__


def test_block_providers_default_blocks_deepinfra(monkeypatch):
    """미설정(키 부재) = DeepInfra 차단. dev 는 OpenRouter 경유라 이 방어가 필요한 유일한 환경."""
    monkeypatch.setenv("MODEL_NAME", "m")
    monkeypatch.delenv("KBP_VL_BLOCK_PROVIDERS", raising=False)
    assert vl_api._build_payload("x", "u", "s")["provider"] == {"ignore": ["DeepInfra"]}


@pytest.mark.parametrize("value", ["", "  ", "none", "NONE", " Off ", "off"])
def test_block_providers_disabled(monkeypatch, value):
    """빈 값·공백·none/off(대소문자 무관) = 끄기 → `provider` 키 자체가 없다.

    빈 값이 끄기인 근거: `docker-compose.yml` 이 **단일 대시** `${VAR-DeepInfra}` 라 빈 값을
    보존한다. `none` 센티널이 따로 있는 근거: `sync-parse-svc-env.sh` 가 빈 값을 버려
    호스트 경로에서는 키가 안 실리고 기본값이 부활한다.
    """
    monkeypatch.setenv("MODEL_NAME", "m")
    monkeypatch.setenv("KBP_VL_BLOCK_PROVIDERS", value)
    assert "provider" not in vl_api._build_payload("x", "u", "s")


def test_block_providers_custom_list(monkeypatch):
    monkeypatch.setenv("MODEL_NAME", "m")
    monkeypatch.setenv("KBP_VL_BLOCK_PROVIDERS", "DeepInfra, Venice")
    assert vl_api._build_payload("x", "u", "s")["provider"] == {"ignore": ["DeepInfra", "Venice"]}


def test_build_page_hybrid_prompts_alias_is_callable():
    """`:762-764` 의 깨진 중복 정의를 지웠다 — 호출 즉시 NameError 였다."""
    from parse_service.parsers.ocr import prompts
    assert prompts.build_page_hybrid_prompts() == prompts.page_hybrid_prompts()
