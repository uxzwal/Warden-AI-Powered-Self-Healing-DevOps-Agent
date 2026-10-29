"""Automated tests for Remediation Engine.

Tests:
1. Permitted remediation actions pass validation.
2. Arbitrary commands, code, and unapproved actions are rejected.
3. Out-of-range configuration parameters are rejected.
4. Unapproved remediation execution is strictly blocked.
5. Applying an approved fix changes the effective service configuration and computes diff.
"""

import pytest
import httpx

from warden.payment_service import app, state
from warden.remediation import (
    RemediationEngine,
    RemediationSecurityError,
    RemediationValidationError,
)
from warden.schemas import RemediationAction


@pytest.fixture(autouse=True)
def reset_service_fixture():
    """Ensure baseline healthy config before/after tests."""
    state.reset_state()
    yield
    state.reset_state()


def test_allowed_actions_pass():
    """Test that all allowlisted actions pass validation."""
    engine = RemediationEngine()
    
    # 1. increase_pool_limit
    p1 = engine.validate_action(RemediationAction.INCREASE_POOL_LIMIT.value, {"max_connections": 50})
    assert p1.max_connections == 50

    # 2. reset_service_config
    p2 = engine.validate_action(RemediationAction.RESET_SERVICE_CONFIG.value, {})
    assert p2 is not None

    # 3. no_op
    p3 = engine.validate_action(RemediationAction.NO_OP.value, {})
    assert p3 is not None


def test_invalid_remediation_actions_rejected():
    """Test that arbitrary shell commands and unapproved actions are rejected."""
    engine = RemediationEngine()
    
    dangerous_actions = [
        "execute_bash",
        "rm -rf /",
        "drop_database",
        "eval_python_code",
        "restart_host",
        "curl_malicious_script",
    ]

    for act in dangerous_actions:
        with pytest.raises(RemediationSecurityError) as exc_info:
            engine.validate_action(act, {})
        assert "SECURITY VIOLATION" in str(exc_info.value)


def test_out_of_range_parameters_rejected():
    """Test that parameters outside safe operational bounds are strictly rejected."""
    engine = RemediationEngine()

    # Pool limit below minimum (min is 5)
    with pytest.raises(RemediationValidationError):
        engine.validate_action("increase_pool_limit", {"max_connections": 2})

    # Pool limit above maximum (max is 200)
    with pytest.raises(RemediationValidationError):
        engine.validate_action("increase_pool_limit", {"max_connections": 9999})

    # Negative processing delay
    with pytest.raises(RemediationValidationError):
        engine.validate_action("increase_pool_limit", {"max_connections": 20, "processing_delay_ms": -50.0})


@pytest.mark.asyncio
async def test_unapproved_remediation_blocked():
    """Test that attempting to apply a fix without explicit approval raises an error."""
    engine = RemediationEngine()
    with pytest.raises(RemediationSecurityError) as exc_info:
        await engine.apply_remediation(
            action="increase_pool_limit",
            parameters={"max_connections": 50},
            is_approved=False
        )
    assert "Explicit operator approval is required" in str(exc_info.value)


@pytest.mark.asyncio
async def test_applying_approved_fix_updates_config(monkeypatch):
    """Test that applying an approved fix modifies configuration and returns diff."""
    transport = httpx.ASGITransport(app=app)

    # Monkeypatch httpx.AsyncClient inside remediation to use in-memory transport
    orig_client = httpx.AsyncClient

    def custom_client(*args, **kwargs):
        kwargs["transport"] = transport
        kwargs["base_url"] = "http://testserver"
        return orig_client(*args, **kwargs)

    monkeypatch.setattr("warden.remediation.httpx.AsyncClient", custom_client)

    engine = RemediationEngine(service_base_url="http://testserver")

    # First throttle to 2 conns
    headers = {"X-Warden-Admin-Token": state.config.admin_token}
    async with custom_client() as c:
        await c.post(
            "/admin/inject-incident",
            json={"severity": "HIGH", "max_connections": 2, "processing_delay_ms": 100.0, "reason": "Test"},
            headers=headers
        )

    # Apply approved remediation to increase to 60 conns
    success, message, diff = await engine.apply_remediation(
        action="increase_pool_limit",
        parameters={"max_connections": 60},
        is_approved=True
    )

    assert success is True
    assert "60 connections" in message
    assert "max_connections" in diff
    assert diff["max_connections"]["before"] == 2
    assert diff["max_connections"]["after"] == 60
    assert state.config.max_connections == 60
