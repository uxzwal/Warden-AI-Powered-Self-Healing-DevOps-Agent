"""Automated tests for Verification Harness.

Tests:
1. Verification measures actual HTTP responses and latency statistics.
2. Degraded state detected when concurrency limits are throttled.
3. Healthy state detected when concurrency pool is adequate.
4. Recovery evaluation strictly requires post-fix verification success.
"""

import pytest
import httpx

from warden.payment_service import app, state
from warden.schemas import VerificationResult
from warden.verifier import ServiceVerifier


@pytest.fixture(autouse=True)
def reset_service_fixture():
    """Ensure baseline healthy config before/after tests."""
    state.reset_state()
    yield
    state.reset_state()


@pytest.mark.asyncio
async def test_verifier_measures_actual_healthy_responses(monkeypatch):
    """Test that verifier accurately measures a healthy service."""
    transport = httpx.ASGITransport(app=app)
    orig_client = httpx.AsyncClient

    def custom_client(*args, **kwargs):
        kwargs["transport"] = transport
        kwargs["base_url"] = "http://testserver"
        return orig_client(*args, **kwargs)

    monkeypatch.setattr("warden.verifier.httpx.AsyncClient", custom_client)

    verifier = ServiceVerifier(service_base_url="http://testserver")
    result = await verifier.run_verification(total_requests=10, concurrency=5)

    assert result.total_requests == 10
    assert result.success_200 == 10
    assert result.rate_limited_503 == 0
    assert result.failure_rate == 0.0
    assert result.status == "HEALTHY"
    assert result.avg_latency_ms >= 0.0


@pytest.mark.asyncio
async def test_verifier_detects_degradation_under_throttling(monkeypatch):
    """Test that verifier accurately measures 503s when service is throttled."""
    transport = httpx.ASGITransport(app=app)
    orig_client = httpx.AsyncClient

    def custom_client(*args, **kwargs):
        kwargs["transport"] = transport
        kwargs["base_url"] = "http://testserver"
        return orig_client(*args, **kwargs)

    monkeypatch.setattr("warden.verifier.httpx.AsyncClient", custom_client)

    # Throttle pool to 1 slot and 150ms delay
    headers = {"X-Warden-Admin-Token": state.config.admin_token}
    async with custom_client() as c:
        await c.post(
            "/admin/inject-incident",
            json={"severity": "CRITICAL", "max_connections": 1, "processing_delay_ms": 150.0, "reason": "Test Throttling"},
            headers=headers
        )

    verifier = ServiceVerifier(service_base_url="http://testserver")
    result = await verifier.run_verification(total_requests=10, concurrency=6)

    assert result.total_requests == 10
    assert result.rate_limited_503 > 0, f"Expected 503 errors, got {result}"
    assert result.failure_rate > 0.0
    assert result.status in ["DEGRADED", "FAILED"]


def test_is_recovered_strict_criteria():
    """Test that recovery is only declared when health criteria are strictly met."""
    verifier = ServiceVerifier()

    baseline_degraded = VerificationResult(
        total_requests=30,
        success_200=5,
        rate_limited_503=25,
        other_errors=0,
        timeouts=0,
        failure_rate=0.833,
        avg_latency_ms=120.0,
        p95_latency_ms=250.0,
        p99_latency_ms=300.0,
        status="FAILED",
        timestamp="2026-10-29T00:00:00Z"
    )

    # Candidate 1: Still failing
    postfix_failing = baseline_degraded.model_copy(update={"rate_limited_503": 10, "failure_rate": 0.33, "status": "DEGRADED"})
    recovered, reason = verifier.is_recovered(baseline_degraded, postfix_failing)
    assert recovered is False
    assert "NOT RECOVERED" in reason

    # Candidate 2: Genuine recovery
    postfix_healthy = VerificationResult(
        total_requests=30,
        success_200=30,
        rate_limited_503=0,
        other_errors=0,
        timeouts=0,
        failure_rate=0.0,
        avg_latency_ms=95.0,
        p95_latency_ms=110.0,
        p99_latency_ms=125.0,
        status="HEALTHY",
        timestamp="2026-10-29T00:01:00Z"
    )
    recovered, reason = verifier.is_recovered(baseline_degraded, postfix_healthy)
    assert recovered is True
    assert "RECOVERED" in reason
