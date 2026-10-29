"""Safe Remediation Engine for Warden AI DevOps Agent.

Strictly enforces:
1. Hard allowlist of permitted remediation actions (increase_pool_limit, reset_service_config, no_op).
2. Explicit parameter range boundaries (e.g., max_connections in [5, 200]).
3. Rejection of any unapproved system commands, code execution, or shell calls.
4. Before-and-after configuration diff generation.
5. Concurrency-safe HTTP updates to the running service with audit trail logging.
"""

import os
from typing import Any, Dict, Optional, Tuple
import httpx
from pydantic import ValidationError

from warden.audit import log_remediation_applied
from warden.schemas import ActionParameters, RemediationAction, ServiceConfig


class RemediationSecurityError(Exception):
    """Raised when an unauthorized, dangerous, or unapproved action is attempted."""
    pass


class RemediationValidationError(Exception):
    """Raised when remediation parameters violate safety bounds."""
    pass


class RemediationEngine:
    """Deterministic, secure engine for executing validated configuration remediations."""

    ALLOWED_ACTIONS = {
        RemediationAction.INCREASE_POOL_LIMIT.value,
        RemediationAction.RESET_SERVICE_CONFIG.value,
        RemediationAction.NO_OP.value,
    }

    def __init__(
        self,
        service_base_url: Optional[str] = None,
        admin_token: Optional[str] = None
    ):
        self.service_base_url = (
            service_base_url
            or os.getenv("WARDEN_SERVICE_URL", "http://localhost:8001")
        ).rstrip("/")
        self.admin_token = admin_token or os.getenv("WARDEN_ADMIN_TOKEN", "warden-admin-secret-token")

    def validate_action(self, action: str, parameters: Dict[str, Any]) -> ActionParameters:
        """Validate proposed remediation action and parameters against safety policies.
        
        Raises:
            RemediationSecurityError: If action is not in strict allowlist.
            RemediationValidationError: If parameters violate numerical bounds.
        """
        if action not in self.ALLOWED_ACTIONS:
            raise RemediationSecurityError(
                f"SECURITY VIOLATION: Action '{action}' is not in the allowed actions list: {sorted(list(self.ALLOWED_ACTIONS))}. "
                "Arbitrary command or script execution is strictly forbidden."
            )

        if action == RemediationAction.NO_OP.value:
            return ActionParameters()

        if action == RemediationAction.RESET_SERVICE_CONFIG.value:
            return ActionParameters()

        if action == RemediationAction.INCREASE_POOL_LIMIT.value:
            try:
                validated_params = ActionParameters(**parameters)
                if validated_params.max_connections is None:
                    # Default safe pool size if not explicitly set
                    validated_params.max_connections = 50
                return validated_params
            except (ValidationError, ValueError) as e:
                raise RemediationValidationError(f"Invalid remediation parameters: {e}") from e

        raise RemediationSecurityError(f"Unsupported action: {action}")

    def compute_diff(self, before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """Compute human-readable configuration diff between before and after states."""
        diff: Dict[str, Dict[str, Any]] = {}
        all_keys = set(before.keys()).union(set(after.keys()))
        for key in all_keys:
            val_before = before.get(key)
            val_after = after.get(key)
            if val_before != val_after:
                diff[key] = {
                    "before": val_before,
                    "after": val_after,
                }
        return diff

    async def get_current_config(self) -> Dict[str, Any]:
        """Fetch current live configuration from payment service."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{self.service_base_url}/config")
            resp.raise_for_status()
            return resp.json()

    async def apply_remediation(
        self,
        action: str,
        parameters: Dict[str, Any],
        is_approved: bool = False
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Apply an approved, safe remediation to the live service via authenticated HTTP.
        
        Args:
            action: Permitted action name.
            parameters: Action parameter dict.
            is_approved: Boolean confirming explicit operator authorization.
            
        Returns:
            Tuple of (success: bool, message: str, diff: dict)
        """
        if not is_approved:
            raise RemediationSecurityError(
                "Remediation blocked: Explicit operator approval is required before applying configuration changes."
            )

        # Enforce validation
        validated_params = self.validate_action(action, parameters)

        if action == RemediationAction.NO_OP.value:
            return True, "No-op remediation: No configuration changes applied.", {}

        headers = {"X-Warden-Admin-Token": self.admin_token}

        async with httpx.AsyncClient(timeout=10.0) as client:
            # 1. Capture before-state
            before_resp = await client.get(f"{self.service_base_url}/config")
            before_resp.raise_for_status()
            before_config = before_resp.json()

            # 2. Execute appropriate safe action
            if action == RemediationAction.RESET_SERVICE_CONFIG.value:
                post_resp = await client.post(
                    f"{self.service_base_url}/admin/reset",
                    headers=headers
                )
                post_resp.raise_for_status()
                after_data = post_resp.json()
                after_config = after_data.get("effective_config", {})
                message = "Service reset to baseline healthy configuration."

            elif action == RemediationAction.INCREASE_POOL_LIMIT.value:
                payload = {
                    "max_connections": validated_params.max_connections,
                    "processing_delay_ms": validated_params.processing_delay_ms,
                    "reason": validated_params.reason or "Warden self-healing remediation"
                }
                post_resp = await client.post(
                    f"{self.service_base_url}/admin/update-config",
                    json=payload,
                    headers=headers
                )
                post_resp.raise_for_status()
                after_data = post_resp.json()
                after_config = after_data.get("effective_config", {})
                message = f"Pool limit updated to {validated_params.max_connections} connections."

            else:
                raise RemediationSecurityError(f"Action '{action}' is unhandled.")

            # 3. Calculate diff
            diff = self.compute_diff(before_config, after_config)

            # 4. Record audit event
            log_remediation_applied(action, before_config, after_config, diff)

            return True, message, diff
