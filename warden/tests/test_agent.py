"""Automated tests for Warden Agent and Nebius Client.

Tests:
1. Demo Mode operates deterministically without any API credentials.
2. Agent diagnosis strictly adheres to Pydantic AgentDiagnosis schema.
3. Malformed LLM output is caught and fails safely with actionable guidance.
4. Model routing distinguishes Fast and Deep models.
5. Tavily client handles absence of credentials gracefully.
"""

import json
import pytest
from unittest.mock import AsyncMock, patch
from pydantic import ValidationError

from warden.agent import WardenAgent
from warden.nebius_client import NebiusAPIError, NebiusClient
from warden.schemas import AgentDiagnosis, ServiceConfig, ServiceMetrics
from warden.tavily_client import TavilyClient


def test_demo_mode_works_without_credentials():
    """Verify Demo Mode executes completely with zero API keys and returns structured diagnosis."""
    agent = WardenAgent()
    
    # Degraded metrics
    metrics = ServiceMetrics(
        total_requests=30,
        successful_requests=5,
        rate_limited_503=25,
        failure_rate=0.833,
        active_connections=2,
        max_connections=2,
        is_degraded=True
    )
    config = ServiceConfig(
        max_connections=2,
        baseline_max_connections=50,
        is_degraded=True,
        incident_description="DB Connection pool throttled"
    )

    diagnosis = agent.generate_demo_diagnosis(metrics, config)

    assert isinstance(diagnosis, AgentDiagnosis)
    assert diagnosis.mode == "demo"
    assert "Simulated" in diagnosis.reasoning_model
    assert diagnosis.action in ["increase_pool_limit", "reset_service_config"]
    assert diagnosis.confidence >= 0.90
    assert len(diagnosis.evidence) > 0
    assert diagnosis.parameters.get("max_connections") == 50


def test_diagnosis_strict_schema_validation():
    """Verify that AgentDiagnosis schema rejects illegal actions or missing fields."""
    valid_data = {
        "incident_summary": "Connection starvation",
        "severity": "CRITICAL",
        "confidence": 0.95,
        "evidence": ["High 503 count"],
        "root_cause_hypothesis": "Pool limit too low",
        "action": "increase_pool_limit",
        "parameters": {"max_connections": 50},
        "expected_outcome": "503 errors drop to 0",
        "verification_plan": "Run 30 requests",
        "explanation": "Increasing pool restores capacity"
    }
    # Valid
    d = AgentDiagnosis(**valid_data)
    assert d.action == "increase_pool_limit"

    # Illegal action
    invalid_data = dict(valid_data)
    invalid_data["action"] = "drop_all_tables"
    with pytest.raises(ValidationError):
        AgentDiagnosis(**invalid_data)

    # Invalid pool limit parameter
    invalid_params = dict(valid_data)
    invalid_params["parameters"] = {"max_connections": 9999}
    with pytest.raises(ValidationError):
        AgentDiagnosis(**invalid_params)


@pytest.mark.asyncio
async def test_malformed_model_output_fails_safely():
    """Verify that when live model returns non-JSON or malformed text, NebiusClient fails safely."""
    client = NebiusClient(api_key="mock_test_key")

    mock_chat = AsyncMock()
    mock_choice = AsyncMock()
    mock_choice.message.content = "Sorry, I cannot answer in JSON: HTTP 503 is bad!"
    mock_chat.choices = [mock_choice]

    with patch("warden.nebius_client.AsyncOpenAI") as mock_openai_cls:
        mock_instance = AsyncMock()
        mock_instance.chat.completions.create.return_value = mock_chat
        mock_openai_cls.return_value = mock_instance

        metrics = ServiceMetrics()
        config = ServiceConfig()

        with pytest.raises(NebiusAPIError) as exc_info:
            await client.analyze_incident(metrics=metrics, config=config, recent_logs=[])

        assert "Malformed JSON" in str(exc_info.value)
        assert exc_info.value.suggestion is not None


def test_tavily_handles_missing_credentials():
    """Verify Tavily client handles absence of credentials without crashing."""
    client = TavilyClient(api_key=None)
    assert client.is_available() is False


def test_model_routing_fast_vs_deep():
    """Verify model routing selects correct configured model IDs."""
    client = NebiusClient(
        api_key="mock_key",
        fast_model="custom/fast-model-8b",
        deep_model="custom/deep-model-70b"
    )
    assert client.get_model_for_mode("fast") == "custom/fast-model-8b"
    assert client.get_model_for_mode("deep") == "custom/deep-model-70b"
