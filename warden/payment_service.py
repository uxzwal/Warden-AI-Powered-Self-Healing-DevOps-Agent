"""Payment Gateway Simulator Service.

A production-grade simulated microservice built with FastAPI.
Implements dynamic concurrency throttling via an asyncio-safe gate,
reproducible incident injection, structured telemetry/metrics,
and authenticated administrative configuration endpoints.

DISCLAIMER: This simulator is designed strictly for DevOps resilience
and AI self-healing demonstrations. It does NOT process real financial transactions.
"""

import asyncio
from collections import deque
from datetime import datetime, timezone
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure package and parent paths are available
ROOT_DIR = str(Path(__file__).resolve().parent.parent)
PKG_DIR = str(Path(__file__).resolve().parent)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
if PKG_DIR not in sys.path:
    sys.path.insert(0, PKG_DIR)

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from warden.schemas import IncidentPayload, ServiceConfig, ServiceMetrics

# Set up logger
logger = logging.getLogger("warden.payment_service")
logging.basicConfig(level=logging.INFO)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


class ConcurrencyLimiter:
    """Thread/asyncio-safe concurrency limiter with dynamic pool limit updating."""

    def __init__(self, max_connections: int):
        self._max_connections = max_connections
        self._active_connections = 0
        self._lock = asyncio.Lock()

    @property
    def max_connections(self) -> int:
        return self._max_connections

    @property
    def active_connections(self) -> int:
        return self._active_connections

    async def try_acquire(self) -> bool:
        async with self._lock:
            if self._active_connections < self._max_connections:
                self._active_connections += 1
                return True
            return False

    async def release(self) -> None:
        async with self._lock:
            if self._active_connections > 0:
                self._active_connections -= 1

    async def set_limit(self, new_limit: int) -> None:
        async with self._lock:
            self._max_connections = max(1, new_limit)


class PaymentServiceState:
    """Encapsulated state for the payment service simulator."""

    def __init__(self):
        self.start_time = time.time()
        self.config: ServiceConfig = self._load_initial_config()
        self.limiter = ConcurrencyLimiter(self.config.max_connections)
        self.total_requests: int = 0
        self.successful_requests: int = 0
        self.rate_limited_503: int = 0
        self.other_errors: int = 0
        self.recent_logs: deque = deque(maxlen=100)
        self._metrics_lock = asyncio.Lock()

    def reset_state(self) -> None:
        """Synchronously reset all configuration and telemetry to baseline healthy state."""
        self.config.max_connections = self.config.baseline_max_connections
        self.config.processing_delay_ms = self.config.baseline_processing_delay_ms
        self.config.is_degraded = False
        self.config.incident_description = None
        self.limiter._max_connections = self.config.baseline_max_connections
        self.limiter._active_connections = 0
        self.total_requests = 0
        self.successful_requests = 0
        self.rate_limited_503 = 0
        self.other_errors = 0
        self.recent_logs.clear()
        self.save_config()

    def _load_initial_config(self) -> ServiceConfig:
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return ServiceConfig(**data)
            except Exception as e:
                logger.warning("Could not parse %s: %s. Using default config.", CONFIG_PATH, e)
        return ServiceConfig()

    def save_config(self) -> None:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                f.write(self.config.model_dump_json(indent=2))
        except Exception as e:
            logger.error("Failed to persist config to %s: %s", CONFIG_PATH, e)

    async def log_event(self, level: str, message: str, endpoint: str = "") -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "endpoint": endpoint,
            "message": message,
            "active_connections": self.limiter.active_connections,
            "max_connections": self.limiter.max_connections,
        }
        async with self._metrics_lock:
            self.recent_logs.append(entry)
        if level == "CRITICAL":
            logger.critical(message)
        elif level == "WARNING":
            logger.warning(message)
        else:
            logger.info(message)


# Initialize service and state
app = FastAPI(
    title="Payment Gateway Simulator",
    description="Simulated resilient microservice for Warden DevOps Self-Healing Agent demo",
    version="1.0.0"
)
state = PaymentServiceState()


def verify_admin_auth(x_warden_admin_token: Optional[str] = Header(None)) -> None:
    """Authenticate administrative actions against configured admin token."""
    expected_token = state.config.admin_token
    if not expected_token:
        return
    if x_warden_admin_token != expected_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Warden-Admin-Token header for admin endpoint"
        )


@app.get("/health", tags=["Telemetry"])
async def get_health():
    """Return health status based on connection pool capacity and error rates."""
    status_label = "DEGRADED" if state.config.is_degraded or state.limiter.max_connections <= 5 else "HEALTHY"
    return {
        "status": status_label,
        "service": state.config.service_name,
        "active_connections": state.limiter.active_connections,
        "max_connections": state.limiter.max_connections,
        "uptime_seconds": round(time.time() - state.start_time, 2),
        "version": "1.0.0",
        "is_degraded": state.config.is_degraded,
    }


@app.get("/metrics", response_model=ServiceMetrics, tags=["Telemetry"])
async def get_metrics():
    """Return detailed real-time operational metrics and recent logs."""
    async with state._metrics_lock:
        tot = state.total_requests
        err = state.rate_limited_503 + state.other_errors
        fail_rate = round(err / tot, 4) if tot > 0 else 0.0

        return ServiceMetrics(
            total_requests=tot,
            successful_requests=state.successful_requests,
            rate_limited_503=state.rate_limited_503,
            other_errors=state.other_errors,
            active_connections=state.limiter.active_connections,
            max_connections=state.limiter.max_connections,
            processing_delay_ms=state.config.processing_delay_ms,
            failure_rate=fail_rate,
            uptime_seconds=round(time.time() - state.start_time, 2),
            is_degraded=state.config.is_degraded,
            recent_logs=list(state.recent_logs),
        )


@app.get("/config", response_model=ServiceConfig, tags=["Configuration"])
async def get_config():
    """Return the active runtime configuration."""
    return state.config


@app.get("/api/v1/payments/charge", tags=["Payments"])
async def charge_payment(
    amount: float = Query(default=49.99, ge=0.01),
    currency: str = Query(default="USD"),
    customer_id: str = Query(default="cust_demo_101")
):
    """Simulated payment charge endpoint with genuine concurrency limits.
    
    Returns HTTP 200 on successful slot acquisition and processing.
    Returns HTTP 503 Service Unavailable when the connection pool is saturated.
    """
    async with state._metrics_lock:
        state.total_requests += 1

    acquired = await state.limiter.try_acquire()
    if not acquired:
        async with state._metrics_lock:
            state.rate_limited_503 += 1

        msg = f"HTTP 503: Connection pool exhausted (active={state.limiter.active_connections}, max={state.limiter.max_connections})"
        await state.log_event("WARNING", msg, endpoint="/api/v1/payments/charge")

        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "error",
                "error_code": "CONNECTION_POOL_EXHAUSTED",
                "message": "Payment gateway saturated. Service temporarily unavailable.",
                "active_connections": state.limiter.active_connections,
                "max_connections": state.limiter.max_connections,
                "timestamp": datetime.now(timezone.utc).isoformat()
            },
            headers={"Retry-After": "1"}
        )

    try:
        # Simulate realistic transaction network and compute latency
        delay_sec = max(0.01, state.config.processing_delay_ms / 1000.0)
        await asyncio.sleep(delay_sec)

        async with state._metrics_lock:
            state.successful_requests += 1

        tx_id = f"txn_{uuid.uuid4().hex[:12]}"
        return {
            "status": "success",
            "transaction_id": tx_id,
            "amount": amount,
            "currency": currency,
            "customer_id": customer_id,
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "active_connections": state.limiter.active_connections,
            "max_connections": state.limiter.max_connections,
            "note": "SIMULATED TRANSACTION ONLY - NO REAL FUNDS INVOLVED"
        }
    except Exception as exc:
        async with state._metrics_lock:
            state.other_errors += 1
        await state.log_event("ERROR", f"Unexpected failure in charge: {exc}", endpoint="/api/v1/payments/charge")
        raise HTTPException(status_code=500, detail="Internal processing error")
    finally:
        await state.limiter.release()


@app.post("/admin/inject-incident", tags=["Administration"])
async def inject_incident(
    payload: IncidentPayload,
    x_warden_admin_token: Optional[str] = Header(None)
):
    """Admin endpoint: injects a controlled incident by throttling connection pool capacity."""
    verify_admin_auth(x_warden_admin_token)

    state.config.max_connections = payload.max_connections
    state.config.processing_delay_ms = payload.processing_delay_ms
    state.config.is_degraded = True
    state.config.incident_description = payload.reason

    await state.limiter.set_limit(payload.max_connections)
    state.save_config()

    incident_msg = (
        f"[INCIDENT INJECTED] Severity: {payload.severity}. "
        f"Pool throttled to {payload.max_connections} conns (delay: {payload.processing_delay_ms}ms). "
        f"Reason: {payload.reason}"
    )
    await state.log_event("CRITICAL", incident_msg, endpoint="/admin/inject-incident")

    return {
        "status": "incident_active",
        "incident_details": payload.model_dump(),
        "effective_config": state.config.model_dump()
    }


@app.post("/admin/reset", tags=["Administration"])
async def reset_service(x_warden_admin_token: Optional[str] = Header(None)):
    """Admin endpoint: resets service config and metrics counters back to baseline healthy state."""
    verify_admin_auth(x_warden_admin_token)

    state.reset_state()
    await state.log_event("INFO", "[SYSTEM RESET] Service configuration and metrics reset to baseline.", endpoint="/admin/reset")

    return {
        "status": "reset_successful",
        "message": "Payment service restored to healthy baseline",
        "effective_config": state.config.model_dump()
    }


class UpdateConfigPayload(BaseModel):
    max_connections: Optional[int] = Field(None, ge=5, le=200)
    processing_delay_ms: Optional[float] = Field(None, ge=10.0, le=5000.0)
    reason: Optional[str] = None


@app.post("/admin/update-config", tags=["Administration"])
async def update_service_config(
    payload: UpdateConfigPayload,
    x_warden_admin_token: Optional[str] = Header(None)
):
    """Admin endpoint: safely applies an approved configuration remediation."""
    verify_admin_auth(x_warden_admin_token)

    if payload.max_connections is not None:
        state.config.max_connections = payload.max_connections
        await state.limiter.set_limit(payload.max_connections)

    if payload.processing_delay_ms is not None:
        state.config.processing_delay_ms = payload.processing_delay_ms

    if state.config.max_connections >= state.config.baseline_max_connections:
        state.config.is_degraded = False
        state.config.incident_description = None

    state.save_config()
    log_msg = f"[CONFIG APPLIED] max_connections={state.config.max_connections}, delay={state.config.processing_delay_ms}ms. Reason: {payload.reason or 'Remediation'}"
    await state.log_event("INFO", log_msg, endpoint="/admin/update-config")

    return {
        "status": "config_updated",
        "message": "Configuration updated successfully",
        "effective_config": state.config.model_dump()
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("warden.payment_service:app", host="127.0.0.1", port=8001, log_level="info")
