"""Verification Harness for Warden AI DevOps Agent.

Dispatches concurrent HTTP requests against the live payment service,
measures actual status codes and round-trip latencies, computes percentiles,
and returns an objective, empirically validated VerificationResult.

NO hardcoded recovery results: status is computed strictly from live responses.
"""

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import httpx
import numpy as np

from warden.audit import log_verification_run
from warden.schemas import VerificationResult


class ServiceVerifier:
    """Automated concurrency test harness and health verification engine."""

    def __init__(self, service_base_url: Optional[str] = None):
        self.service_base_url = (
            service_base_url
            or os.getenv("WARDEN_SERVICE_URL", "http://localhost:8001")
        ).rstrip("/")

    async def _send_single_request(
        self,
        client: httpx.AsyncClient,
        request_idx: int,
        results_list: List[Dict[str, Any]]
    ) -> None:
        """Execute a single HTTP payment request and record status and roundtrip latency."""
        t_start = time.perf_counter()
        try:
            resp = await client.get(
                f"{self.service_base_url}/api/v1/payments/charge",
                params={"amount": 49.99, "customer_id": f"verifier_req_{request_idx}"}
            )
            elapsed_ms = (time.perf_counter() - t_start) * 1000.0
            results_list.append({
                "status_code": resp.status_code,
                "latency_ms": elapsed_ms,
                "timeout": False,
                "error": None
            })
        except httpx.TimeoutException:
            elapsed_ms = (time.perf_counter() - t_start) * 1000.0
            results_list.append({
                "status_code": 0,
                "latency_ms": elapsed_ms,
                "timeout": True,
                "error": "Timeout"
            })
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - t_start) * 1000.0
            results_list.append({
                "status_code": 0,
                "latency_ms": elapsed_ms,
                "timeout": False,
                "error": str(exc)
            })

    async def run_verification(
        self,
        total_requests: int = 30,
        concurrency: int = 15,
        timeout_seconds: float = 4.0,
        phase: str = "baseline"
    ) -> VerificationResult:
        """Execute concurrent load test against /api/v1/payments/charge and evaluate health.
        
        Args:
            total_requests: Total number of HTTP charge requests to fire.
            concurrency: Number of parallel tasks executing requests simultaneously.
            timeout_seconds: HTTP client timeout per request.
            phase: 'baseline' or 'post_remediation' (for audit logging).
            
        Returns:
            Empirical VerificationResult.
        """
        raw_results: List[Dict[str, Any]] = []
        semaphore = asyncio.Semaphore(concurrency)

        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            async def bounded_worker(idx: int):
                async with semaphore:
                    await self._send_single_request(client, idx, raw_results)

            tasks = [asyncio.create_task(bounded_worker(i)) for i in range(total_requests)]
            await asyncio.gather(*tasks)

        # Aggregate statistics
        success_200 = sum(1 for r in raw_results if r["status_code"] == 200)
        rate_limited_503 = sum(1 for r in raw_results if r["status_code"] == 503)
        timeouts = sum(1 for r in raw_results if r.get("timeout", False))
        other_errors = len(raw_results) - (success_200 + rate_limited_503 + timeouts)
        total = len(raw_results)

        failed_requests = rate_limited_503 + timeouts + other_errors
        failure_rate = round(failed_requests / total, 4) if total > 0 else 1.0

        latencies = [r["latency_ms"] for r in raw_results if r["status_code"] == 200 or r["status_code"] == 503]
        if latencies:
            avg_lat = float(np.mean(latencies))
            p95_lat = float(np.percentile(latencies, 95))
            p99_lat = float(np.percentile(latencies, 99))
        else:
            avg_lat = 0.0
            p95_lat = 0.0
            p99_lat = 0.0

        # Determine overall health status
        if failure_rate == 0.0:
            status_verdict = "HEALTHY"
            details = f"All {total} requests succeeded (0.0% failure rate). Service is healthy."
        elif failure_rate <= 0.10:
            status_verdict = "DEGRADED"
            details = f"Minor degradation: {failure_rate * 100:.1f}% failure rate ({rate_limited_503} HTTP 503s)."
        else:
            status_verdict = "FAILED" if failure_rate >= 0.50 else "DEGRADED"
            details = (
                f"Severe degradation: {failure_rate * 100:.1f}% failure rate "
                f"({rate_limited_503} HTTP 503s, {timeouts} timeouts)."
            )

        verification_result = VerificationResult(
            total_requests=total,
            success_200=success_200,
            rate_limited_503=rate_limited_503,
            other_errors=other_errors,
            timeouts=timeouts,
            failure_rate=failure_rate,
            avg_latency_ms=round(avg_lat, 2),
            p95_latency_ms=round(p95_lat, 2),
            p99_latency_ms=round(p99_lat, 2),
            status=status_verdict,
            timestamp=datetime.now(timezone.utc).isoformat(),
            details=details,
        )

        # Record to audit trail
        log_verification_run(phase, verification_result.model_dump())

        return verification_result

    def is_recovered(self, baseline: Optional[VerificationResult], post_fix: VerificationResult) -> Tuple[bool, str]:
        """Strictly evaluate if an incident has recovered.
        
        Criteria:
        - Post-fix status must be HEALTHY.
        - Failure rate must be 0.0%.
        - If baseline existed with failures, post-fix must demonstrate actual recovery.
        """
        if post_fix.status == "HEALTHY" and post_fix.failure_rate == 0.0:
            if baseline and baseline.failure_rate > 0:
                return True, f"RECOVERED: Failure rate dropped from {baseline.failure_rate*100:.1f}% to 0.0%."
            return True, "RECOVERED: Service is fully healthy and accepting full concurrency load."
        
        return False, f"NOT RECOVERED: Service remains in {post_fix.status} state with {post_fix.failure_rate*100:.1f}% failures."
