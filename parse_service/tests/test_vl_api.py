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
