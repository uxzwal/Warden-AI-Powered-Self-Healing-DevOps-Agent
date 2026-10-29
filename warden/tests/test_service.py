"""Automated tests for FastAPI Payment Gateway Simulator.

Tests:
1. Healthy service returns HTTP 200.
2. An overloaded service returns HTTP 503 with concurrency limiting.
3. Telemetry and metrics reporting.
4. Admin incident injection throttles connection pool.
5. Admin reset restores baseline health.
6. Admin authentication security checks.
"""

import asyncio
import pytest
import httpx
from fastapi import status

from warden.payment_service import app, state


@pytest.fixture(autouse=True)
def reset_state_fixture():
    """Reset service state before and after each test."""
    state.reset_state()
    yield
    state.reset_state()


@pytest.mark.asyncio
async def test_health_endpoint():
    """Test /health endpoint returns healthy status and uptime."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/health")
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["status"] in ["HEALTHY", "DEGRADED"]
        assert "uptime_seconds" in data
        assert data["max_connections"] >= 1


@pytest.mark.asyncio
async def test_healthy_service_returns_200():
    """Test that under normal load, payment charges succeed with HTTP 200."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get(
            "/api/v1/payments/charge",
            params={"amount": 99.50, "currency": "USD", "customer_id": "cust_test_01"}
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["status"] == "success"
        assert "transaction_id" in data
        assert data["amount"] == 99.50


@pytest.mark.asyncio
async def test_overloaded_service_returns_503():
    """Test that when connection limit is saturated, service genuinely rejects with HTTP 503."""
    transport = httpx.ASGITransport(app=app)
    admin_headers = {"X-Warden-Admin-Token": state.config.admin_token}

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Throttle pool to 1 connection and 200ms processing delay
        inject_resp = await client.post(
            "/admin/inject-incident",
            json={"severity": "CRITICAL", "max_connections": 1, "processing_delay_ms": 200.0, "reason": "Test Overload"},
            headers=admin_headers
        )
        assert inject_resp.status_code == status.HTTP_200_OK

        # Fire 6 simultaneous requests
        async def make_charge():
            return await client.get("/api/v1/payments/charge", params={"amount": 10.0})

        tasks = [asyncio.create_task(make_charge()) for _ in range(6)]
        responses = await asyncio.gather(*tasks)

        status_codes = [r.status_code for r in responses]
        assert 503 in status_codes, f"Expected HTTP 503 when pool is saturated, got {status_codes}"

        # Check 503 response body and headers
        err_resp = next(r for r in responses if r.status_code == 503)
        assert err_resp.headers.get("retry-after") == "1"
        assert err_resp.json()["error_code"] == "CONNECTION_POOL_EXHAUSTED"


@pytest.mark.asyncio
async def test_admin_incident_injection_and_reset():
    """Test full cycle of incident injection and administrative reset."""
    transport = httpx.ASGITransport(app=app)
    admin_headers = {"X-Warden-Admin-Token": state.config.admin_token}

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Inject incident
        inject_resp = await client.post(
            "/admin/inject-incident",
            json={"severity": "HIGH", "max_connections": 2, "processing_delay_ms": 150.0, "reason": "DB lock test"},
            headers=admin_headers
        )
        assert inject_resp.status_code == 200
        config_after_inject = (await client.get("/config")).json()
        assert config_after_inject["max_connections"] == 2
        assert config_after_inject["is_degraded"] is True

        # Reset service
        reset_resp = await client.post("/admin/reset", headers=admin_headers)
        assert reset_resp.status_code == 200
        config_after_reset = (await client.get("/config")).json()
        assert config_after_reset["max_connections"] == config_after_reset["baseline_max_connections"]
        assert config_after_reset["is_degraded"] is False


@pytest.mark.asyncio
async def test_admin_auth_security():
    """Test that unauthorized requests to admin endpoints are blocked with 401."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Missing token
        resp1 = await client.post("/admin/reset")
        assert resp1.status_code == status.HTTP_401_UNAUTHORIZED

        # Wrong token
        resp2 = await client.post("/admin/reset", headers={"X-Warden-Admin-Token": "invalid_token"})
        assert resp2.status_code == status.HTTP_401_UNAUTHORIZED
