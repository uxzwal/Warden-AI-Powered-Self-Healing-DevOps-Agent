"""Warden AI DevOps Agent.

Orchestrates incident evidence gathering, multi-model reasoning routing (Fast vs Deep),
optional Tavily technical documentation research, and structured remediation generation.
Operates in two modes:
1. Demo Mode: Deterministic, transparent, zero-API-key simulation on real live services.
2. Live AI Mode: High-performance inference via Nebius Token Factory (Llama 3.1 8B/70B).
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple
from dotenv import load_dotenv
import httpx

load_dotenv(override=False)
for p in [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env", Path(__file__).resolve().parent.parent / ".env"]:
    if p.exists():
        load_dotenv(dotenv_path=str(p), override=False)

from warden.audit import log_diagnosis_performed, record_audit_event
from warden.nebius_client import NebiusAPIError, NebiusClient
from warden.schemas import AgentDiagnosis, RemediationAction, ServiceConfig, ServiceMetrics
from warden.tavily_client import TavilyClient


class WardenAgent:
    """Core autonomous DevOps agent driving incident detection, diagnosis, and remediation planning."""

    def __init__(
        self,
        service_base_url: Optional[str] = None,
        nebius_client: Optional[NebiusClient] = None,
        tavily_client: Optional[TavilyClient] = None,
    ):
        self.service_base_url = (
            service_base_url
            or os.getenv("WARDEN_SERVICE_URL", "http://localhost:8001")
        ).rstrip("/")
        self.nebius_client = nebius_client or NebiusClient()
        self.tavily_client = tavily_client or TavilyClient()

    async def fetch_service_telemetry(self) -> Tuple[ServiceMetrics, ServiceConfig]:
        """Fetch real-time metrics and config directly from the payment service over HTTP."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            metrics_resp = await client.get(f"{self.service_base_url}/metrics")
            metrics_resp.raise_for_status()
            metrics = ServiceMetrics(**metrics_resp.json())

            config_resp = await client.get(f"{self.service_base_url}/config")
            config_resp.raise_for_status()
            config = ServiceConfig(**config_resp.json())

            return metrics, config

    async def conduct_research_if_needed(
        self,
        query: str = "FastAPI HTTP 503 connection pool exhaustion mitigation",
        force: bool = False
    ) -> Optional[List[Dict[str, Any]]]:
        """Optionally retrieve technical documentation from Tavily as advisory context."""
        if not self.tavily_client.is_available():
            return None

        search_res = await self.tavily_client.search(query=query, max_results=3)
        if search_res.get("available") and search_res.get("results"):
            return search_res["results"]
        return None

    def generate_demo_diagnosis(
        self,
        metrics: ServiceMetrics,
        config: ServiceConfig,
        tavily_research: Optional[List[Dict[str, Any]]] = None
    ) -> AgentDiagnosis:
        """Generate deterministic, realistic diagnosis based on real observed service metrics.
        
        Strictly labeled as Demo Deterministic Agent (Simulated).
        Never pretends to be an LLM output.
        """
        is_degraded = config.is_degraded or metrics.max_connections <= 5 or metrics.rate_limited_503 > 0

        if is_degraded:
            evidences = [
                f"Connection pool maximum limit is throttled to {metrics.max_connections} concurrent connections (baseline: {config.baseline_max_connections})",
                f"Service reported {metrics.rate_limited_503} HTTP 503 'Service Unavailable' responses ({metrics.failure_rate * 100:.1f}% failure rate)",
                f"Active connections currently at {metrics.active_connections}/{metrics.max_connections}",
            ]
            if config.incident_description:
                evidences.append(f"Recorded incident trigger: {config.incident_description}")
            if metrics.recent_logs:
                last_err = [l["message"] for l in metrics.recent_logs if l.get("level") in ["WARNING", "CRITICAL"]]
                if last_err:
                    evidences.append(f"Recent log alert: '{last_err[-1]}'")

            diagnosis = AgentDiagnosis(
                incident_summary="Severe connection pool starvation causing elevated HTTP 503 errors on payment transactions.",
                severity="CRITICAL" if metrics.failure_rate > 0.4 else "HIGH",
                confidence=0.96,
                evidence=evidences,
                root_cause_hypothesis=(
                    f"Concurrency bottleneck: The payment gateway's connection limiter is restricted to {metrics.max_connections} "
                    f"concurrent slots with a {config.processing_delay_ms:.0f}ms processing latency. "
                    "Incoming requests exceed available capacity, triggering immediate HTTP 503 rejections."
                ),
                action=RemediationAction.INCREASE_POOL_LIMIT.value,
                parameters={
                    "max_connections": config.baseline_max_connections,
                    "processing_delay_ms": config.baseline_processing_delay_ms,
                    "reason": "Restoring connection pool to baseline healthy capacity"
                },
                expected_outcome=(
                    f"Expands connection pool from {metrics.max_connections} to {config.baseline_max_connections} concurrent slots, "
                    "allowing concurrent transactions to process without HTTP 503 throttling."
                ),
                verification_plan=(
                    f"Execute automated load verification of 30 concurrent requests to /api/v1/payments/charge "
                    "and verify a 0.0% failure rate with 100% HTTP 200 OK responses."
                ),
                explanation=(
                    "Deterministic analysis of real metrics revealed connection pool exhaustion as the primary bottleneck. "
                    f"Increasing max_connections to {config.baseline_max_connections} will restore throughput."
                ),
                mode="demo",
                reasoning_model="Demo Deterministic Agent (Simulated)",
                tavily_research=tavily_research
            )
        else:
            diagnosis = AgentDiagnosis(
                incident_summary="Payment Gateway is operating normally with healthy throughput.",
                severity="LOW",
                confidence=0.99,
                evidence=[
                    f"Zero HTTP 503 errors observed across {metrics.total_requests} requests",
                    f"Active connections ({metrics.active_connections}) within safe limits ({metrics.max_connections})",
                    "Service telemetry status: HEALTHY"
                ],
                root_cause_hypothesis="No active degradation or failure detected. System parameters are within nominal thresholds.",
                action=RemediationAction.NO_OP.value,
                parameters={},
                expected_outcome="No changes required. Service will maintain healthy baseline operation.",
                verification_plan="Periodically monitor /health and /metrics endpoints.",
                explanation="No remediation required because the service is operating normally.",
                mode="demo",
                reasoning_model="Demo Deterministic Agent (Simulated)",
                tavily_research=tavily_research
            )

        # Record in audit trail
        log_diagnosis_performed(diagnosis.model_dump(), mode="demo", model_name="Demo Deterministic Agent (Simulated)")
        return diagnosis

    async def diagnose(
        self,
        mode: Literal["demo", "live"] = "demo",
        reasoning_mode: Literal["fast", "deep"] = "fast",
        include_research: bool = False
    ) -> AgentDiagnosis:
        """Run diagnostic workflow, collecting evidence and producing structured remediation proposal.
        
        Args:
            mode: 'demo' for credential-free deterministic agent, 'live' for Nebius API.
            reasoning_mode: 'fast' or 'deep' model routing.
            include_research: whether to fetch Tavily research.
            
        Returns:
            Validated AgentDiagnosis.
            
        Raises:
            NebiusAPIError: If Live Mode fails (caller should catch and suggest Demo Mode).
        """
        # 1. Fetch real service state
        metrics, config = await self.fetch_service_telemetry()

        # 2. External research (optional)
        tavily_context = None
        if include_research and self.tavily_client.is_available():
            tavily_context = await self.conduct_research_if_needed()

        # 3. Route according to mode
        if mode == "demo":
            return self.generate_demo_diagnosis(metrics, config, tavily_research=tavily_context)

        # Live AI Mode
        selected_model = self.nebius_client.get_model_for_mode(reasoning_mode)
        try:
            diagnosis = await self.nebius_client.analyze_incident(
                metrics=metrics,
                config=config,
                recent_logs=metrics.recent_logs,
                reasoning_mode=reasoning_mode,
                tavily_context=tavily_context
            )
            # Record audit trail
            log_diagnosis_performed(diagnosis.model_dump(), mode="live", model_name=selected_model)
            return diagnosis
        except NebiusAPIError as err:
            record_audit_event(
                event_type="AI_DIAGNOSIS_FAILED",
                actor="AI_AGENT",
                summary=f"Live AI reasoning failed with model '{selected_model}': {err.message}",
                details={"error": str(err), "suggestion": err.suggestion},
                status="ERROR"
            )
            raise
