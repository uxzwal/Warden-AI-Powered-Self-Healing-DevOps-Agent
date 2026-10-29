"""Structured audit logging and event tracking for Warden.

Provides immutable timeline tracking for regulatory compliance, post-mortem analysis,
and real-time visualization on the Streamlit dashboard.
"""

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from warden.schemas import AuditEvent

# Configure logger
logger = logging.getLogger("warden.audit")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        '{"timestamp": "%(asctime)s", "level": "%(levelname)s", "logger": "%(name)s", "message": %(message)s}'
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

AUDIT_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit.log")

_EVENT_STORE_LOCK = threading.Lock()
_IN_MEMORY_EVENTS: List[AuditEvent] = []


def record_audit_event(
    event_type: str,
    actor: str,
    summary: str,
    details: Optional[Dict[str, Any]] = None,
    status: str = "INFO"
) -> AuditEvent:
    """Record a structured audit event to in-memory store and JSON audit log file."""
    now_iso = datetime.now(timezone.utc).isoformat()
    event_id = f"evt_{uuid.uuid4().hex[:10]}"
    event_details = details or {}

    event = AuditEvent(
        id=event_id,
        timestamp=now_iso,
        event_type=event_type,
        actor=actor,
        summary=summary,
        details=event_details,
        status=status,
    )

    with _EVENT_STORE_LOCK:
        _IN_MEMORY_EVENTS.append(event)

    # Persist to JSON-lines audit file
    try:
        with open(AUDIT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(event.model_dump_json() + "\n")
    except Exception as e:
        logger.warning("Failed to write event to %s: %s", AUDIT_LOG_FILE, e)

    # Log structured message
    try:
        log_payload = json.dumps({
            "event_id": event.id,
            "event_type": event.event_type,
            "actor": event.actor,
            "summary": event.summary,
            "status": event.status
        })
        logger.info(log_payload)
    except Exception:
        pass

    return event


def get_audit_events() -> List[AuditEvent]:
    """Return all audit events ordered chronologically."""
    with _EVENT_STORE_LOCK:
        return list(_IN_MEMORY_EVENTS)


def clear_audit_events() -> None:
    """Clear in-memory and disk audit log (used on demo reset)."""
    with _EVENT_STORE_LOCK:
        _IN_MEMORY_EVENTS.clear()
    try:
        if os.path.exists(AUDIT_LOG_FILE):
            open(AUDIT_LOG_FILE, "w").close()
    except Exception:
        pass


# Domain-specific logging convenience helpers

def log_incident_injected(severity: str, max_connections: int, reason: str) -> AuditEvent:
    return record_audit_event(
        event_type="INCIDENT_INJECTED",
        actor="SIMULATOR",
        summary=f"Incident injected: Throttled max_connections to {max_connections}. Severity: {severity}",
        details={"severity": severity, "max_connections": max_connections, "reason": reason},
        status="WARNING" if severity in ["LOW", "MEDIUM"] else "CRITICAL"
    )


def log_diagnosis_performed(diagnosis_data: Dict[str, Any], mode: str, model_name: Optional[str]) -> AuditEvent:
    action = diagnosis_data.get("action", "unknown")
    conf = diagnosis_data.get("confidence", 0.0)
    return record_audit_event(
        event_type="AI_DIAGNOSIS_COMPLETED",
        actor="AI_AGENT",
        summary=f"AI agent ({mode} mode) diagnosed root cause with {conf * 100:.1f}% confidence. Proposed action: {action}",
        details={
            "mode": mode,
            "model": model_name,
            "confidence": conf,
            "root_cause": diagnosis_data.get("root_cause_hypothesis"),
            "proposed_action": action,
            "parameters": diagnosis_data.get("parameters", {}),
            "expected_outcome": diagnosis_data.get("expected_outcome")
        },
        status="SUCCESS"
    )


def log_external_research(query: str, result_count: int, sources: List[Dict[str, str]]) -> AuditEvent:
    return record_audit_event(
        event_type="TAVILY_RESEARCH_COMPLETED",
        actor="TAVILY_RESEARCHER",
        summary=f"External documentation retrieved for query: '{query}' ({result_count} findings)",
        details={"query": query, "sources": sources},
        status="INFO"
    )


def log_remediation_approval(action: str, operator_identity: str = "DevOps_Engineer") -> AuditEvent:
    return record_audit_event(
        event_type="REMEDIATION_APPROVED",
        actor="HUMAN_OPERATOR",
        summary=f"Operator '{operator_identity}' approved remediation action: {action}",
        details={"approved_action": action, "operator": operator_identity},
        status="SUCCESS"
    )


def log_remediation_applied(action: str, before_config: Dict[str, Any], after_config: Dict[str, Any], diff: Dict[str, Any]) -> AuditEvent:
    return record_audit_event(
        event_type="REMEDIATION_APPLIED",
        actor="REMEDIATION_ENGINE",
        summary=f"Remediation applied successfully: {action}. Config updated.",
        details={"action": action, "before": before_config, "after": after_config, "diff": diff},
        status="SUCCESS"
    )


def log_verification_run(phase: str, result_dict: Dict[str, Any]) -> AuditEvent:
    status = result_dict.get("status", "UNKNOWN")
    failure_rate = result_dict.get("failure_rate", 0.0)
    return record_audit_event(
        event_type=f"VERIFICATION_{phase.upper()}",
        actor="VERIFICATION_HARNESS",
        summary=f"{phase.capitalize()} verification finished: Status {status} ({failure_rate*100:.1f}% failure rate)",
        details=result_dict,
        status="SUCCESS" if status == "HEALTHY" else ("WARNING" if status == "DEGRADED" else "ERROR")
    )


def log_service_reset() -> AuditEvent:
    return record_audit_event(
        event_type="SERVICE_RESET",
        actor="OPERATOR",
        summary="Service configuration and error counters reset to baseline health",
        details={},
        status="INFO"
    )
