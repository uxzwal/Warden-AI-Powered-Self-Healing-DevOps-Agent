"""Warden: AI-Powered Self-Healing DevOps Agent Dashboard.

Streamlit frontend providing end-to-end incident injection, AI diagnostic analysis,
human-in-the-loop remediation approval, concurrency verification, and immutable audit trails.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv
import pandas as pd
import streamlit as st

# Ensure root and package directories are in sys.path regardless of launch directory
ROOT_DIR = str(Path(__file__).resolve().parent.parent)
PKG_DIR = str(Path(__file__).resolve().parent)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
if PKG_DIR not in sys.path:
    sys.path.insert(0, PKG_DIR)

# Load environment variables defensively from multiple candidate paths
load_dotenv(override=False)
for p in [Path(ROOT_DIR) / ".env", Path(PKG_DIR) / ".env", Path(ROOT_DIR) / ".env.example"]:
    if p.exists() and p.is_file():
        load_dotenv(dotenv_path=str(p), override=False)

# Configure Streamlit page layout
st.set_page_config(
    page_title="Warden // Autonomous DevOps Agent",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Apply sleek modern DevOps dark theme styling
st.markdown("""
<style>
    /* Dark Theme Core Styles */
    .stApp {
        background-color: #0b0f19;
        color: #e6edf3;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }
    
    /* Header Card */
    .warden-header {
        background: linear-gradient(135deg, #161f30 0%, #0d1322 100%);
        border: 1px solid #23314d;
        border-radius: 12px;
        padding: 20px 24px;
        margin-bottom: 24px;
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
    }
    
    /* Badges */
    .badge {
        display: inline-block;
        padding: 4px 10px;
        border-radius: 6px;
        font-size: 12px;
        font-weight: 600;
        letter-spacing: 0.5px;
        text-transform: uppercase;
    }
    .badge-healthy {
        background-color: rgba(46, 160, 67, 0.15);
        color: #3fb950;
        border: 1px solid rgba(46, 160, 67, 0.4);
    }
    .badge-degraded {
        background-color: rgba(248, 81, 73, 0.15);
        color: #ff7b72;
        border: 1px solid rgba(248, 81, 73, 0.4);
    }
    .badge-recovered {
        background-color: rgba(163, 113, 247, 0.15);
        color: #d2a8ff;
        border: 1px solid rgba(163, 113, 247, 0.4);
    }
    .badge-demo {
        background-color: rgba(210, 153, 34, 0.15);
        color: #e3b341;
        border: 1px solid rgba(210, 153, 34, 0.4);
    }
    .badge-live {
        background-color: rgba(88, 166, 255, 0.15);
        color: #58a6ff;
        border: 1px solid rgba(88, 166, 255, 0.4);
    }

    /* KPI Metric Cards */
    .kpi-card {
        background-color: #121826;
        border: 1px solid #212c42;
        border-radius: 10px;
        padding: 16px;
        text-align: left;
        box-shadow: 0 2px 10px rgba(0,0,0,0.2);
    }
    .kpi-label {
        font-size: 12px;
        color: #8b949e;
        text-transform: uppercase;
        font-weight: 600;
        letter-spacing: 0.5px;
    }
    .kpi-val {
        font-size: 26px;
        font-weight: 700;
        color: #f0f6fc;
        margin: 4px 0;
    }
    .kpi-sub {
        font-size: 11px;
        color: #6e7681;
    }

    /* Terminal Log View */
    .terminal-box {
        background-color: #05080f;
        border: 1px solid #1b2333;
        border-radius: 8px;
        padding: 14px;
        font-family: 'JetBrains Mono', 'Fira Code', 'Courier New', Courier, monospace;
        font-size: 12px;
        color: #7ee787;
        max-height: 280px;
        overflow-y: auto;
        line-height: 1.5;
    }
    .log-critical { color: #ff7b72; font-weight: bold; }
    .log-warning { color: #f2cc60; }
    .log-info { color: #79c0ff; }
</style>
""", unsafe_allow_html=True)

# Imports from warden modules
from warden.agent import WardenAgent
from warden.audit import clear_audit_events, get_audit_events, log_incident_injected, log_service_reset, log_remediation_approval
from warden.nebius_client import NebiusAPIError, NebiusClient
from warden.remediation import RemediationEngine, RemediationSecurityError, RemediationValidationError
from warden.schemas import IncidentPayload, ServiceConfig, ServiceMetrics, VerificationResult
from warden.tavily_client import TavilyClient
from warden.verifier import ServiceVerifier

SERVICE_URL = os.getenv("WARDEN_SERVICE_URL", "http://localhost:8001")
ADMIN_TOKEN = os.getenv("WARDEN_ADMIN_TOKEN", "warden-admin-secret-token")

# Initialize Session State
if "mode" not in st.session_state:
    st.session_state.mode = "demo"
if "reasoning_mode" not in st.session_state:
    st.session_state.reasoning_mode = "fast"
if "baseline_result" not in st.session_state:
    st.session_state.baseline_result = None
if "postfix_result" not in st.session_state:
    st.session_state.postfix_result = None
if "latest_diagnosis" not in st.session_state:
    st.session_state.latest_diagnosis = None
if "operator_approved" not in st.session_state:
    st.session_state.operator_approved = False
if "remediation_applied" not in st.session_state:
    st.session_state.remediation_applied = False
if "last_diff" not in st.session_state:
    st.session_state.last_diff = None
if "incident_status" not in st.session_state:
    st.session_state.incident_status = "HEALTHY"


# Helper: Async runner for Streamlit
def run_sync(coro):
    return asyncio.run(coro)


# Helper: Service status check
def fetch_telemetry_safe() -> tuple[Optional[ServiceMetrics], Optional[ServiceConfig]]:
    try:
        agent = WardenAgent(service_base_url=SERVICE_URL)
        return run_sync(agent.fetch_service_telemetry())
    except Exception:
        return None, None


# Helper: Start background service if not running
def auto_start_service_if_offline():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect(("127.0.0.1", 8001))
        s.close()
        return True
    except ConnectionRefusedError:
        pass
    
    # Try starting it via subprocess
    try:
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "warden.payment_service:app", "--port", "8001", "--host", "127.0.0.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        time.sleep(1.0)
        return True
    except Exception as e:
        st.error(f"Could not auto-start payment service: {e}")
        return False


auto_start_service_if_offline()
metrics, config = fetch_telemetry_safe()
is_service_online = metrics is not None


# ==========================================
# HEADER & SIDEBAR CONTROLS
# ==========================================
with st.sidebar:
    st.markdown("### ⚙️ Warden Settings")
    
    # Operating Mode Toggle
    mode_selection = st.radio(
        "Operating Mode",
        options=["Demo Mode", "Live AI Mode"],
        index=0 if st.session_state.mode == "demo" else 1,
        help="Demo Mode works with zero API keys. Live AI Mode connects to Nebius Token Factory."
    )
    st.session_state.mode = "demo" if mode_selection == "Demo Mode" else "live"

    # Reasoning Mode
    st.markdown("---")
    st.markdown("### 🧠 AI Model Routing")
    reasoning_selection = st.radio(
        "Reasoning Profile",
        options=["⚡ Fast Model", "🧠 Deep Reasoning"],
        index=0 if st.session_state.reasoning_mode == "fast" else 1,
        help="Fast routes to Llama-3.1-8B. Deep routes to Llama-3.1-70B or DeepSeek R1."
    )
    st.session_state.reasoning_mode = "fast" if "Fast" in reasoning_selection else "deep"

    nebius_client = NebiusClient()
    tavily_client = TavilyClient()

    if st.session_state.mode == "live":
        st.markdown("**Live API Status:**")
        if nebius_client.is_available():
            active_model = nebius_client.get_model_for_mode(st.session_state.reasoning_mode)
            st.success(f"Nebius API: Connected\nModel: `{active_model}`")
            with st.expander("🔍 Discover Nebius Account Models"):
                avail_models = run_sync(nebius_client.list_available_models())
                if avail_models:
                    st.caption("Active models available on your Nebius endpoint:")
                    for m in avail_models[:6]:
                        st.code(m, language="text")
        else:
            st.error("NEBIUS_API_KEY not configured or invalid. Falling back to Demo Mode recommended.")

    st.markdown(f"**Tavily Search API:** {'✅ Active' if tavily_client.is_available() else '⚪ Optional (Inactive)'}")
    st.markdown(f"**Payment Service:** {'🟢 Online (:8001)' if is_service_online else '🔴 Offline'}")

    st.markdown("---")
    st.markdown("### 🧪 Quick Actions")
    if st.button("🔄 Reset Demo & Service State", use_container_width=True):
        if is_service_online:
            import httpx
            try:
                run_sync(httpx.AsyncClient().post(f"{SERVICE_URL}/admin/reset", headers={"X-Warden-Admin-Token": ADMIN_TOKEN}))
            except Exception:
                pass
        clear_audit_events()
        log_service_reset()
        st.session_state.baseline_result = None
        st.session_state.postfix_result = None
        st.session_state.latest_diagnosis = None
        st.session_state.operator_approved = False
        st.session_state.remediation_applied = False
        st.session_state.last_diff = None
        st.session_state.incident_status = "HEALTHY"
        st.rerun()


# ==========================================
# MAIN DASHBOARD HEADER
# ==========================================
header_col1, header_col2 = st.columns([3, 2])
with header_col1:
    st.markdown("""
    <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 8px;">
        <span style="font-size: 32px;">🛡️</span>
        <div>
            <h1 style="margin: 0; font-size: 26px; font-weight: 800; letter-spacing: -0.5px; color: #f0f6fc;">WARDEN</h1>
            <p style="margin: 0; font-size: 13px; color: #8b949e;">Autonomous Self-Healing DevOps & SRE Incident Agent</p>
        </div>
    </div>
    """, unsafe_allow_html=True)

with header_col2:
    mode_badge = '<span class="badge badge-demo">Demo Mode (Simulated)</span>' if st.session_state.mode == "demo" else '<span class="badge badge-live">Live Nebius AI</span>'
    
    if not is_service_online:
        health_badge = '<span class="badge badge-degraded">Service Offline</span>'
    elif st.session_state.incident_status == "RECOVERED":
        health_badge = '<span class="badge badge-recovered">Recovered (Self-Healed)</span>'
    elif config and config.is_degraded:
        health_badge = '<span class="badge badge-degraded">Incident Active (Degraded)</span>'
    else:
        health_badge = '<span class="badge badge-healthy">Service Nominal (Healthy)</span>'

    st.markdown(f"""
    <div style="text-align: right; padding-top: 6px;">
        {mode_badge} &nbsp; {health_badge}
        <div style="font-size: 11px; color: #8b949e; margin-top: 6px;">
            Target Service: <code>{SERVICE_URL}</code> | Concurrency Gate Active
        </div>
    </div>
    """, unsafe_allow_html=True)

st.markdown("<hr style='border: none; border-top: 1px solid #212c42; margin: 12px 0 20px 0;'>", unsafe_allow_html=True)


# ==========================================
# OVERVIEW KPI METRICS
# ==========================================
k1, k2, k3, k4 = st.columns(4)

curr_status = "HEALTHY" if (config and not config.is_degraded) else "DEGRADED"
if st.session_state.incident_status == "RECOVERED":
    curr_status = "RECOVERED"

with k1:
    st.markdown(f"""
    <div class="kpi-card">
        <div class="kpi-label">Service Status</div>
        <div class="kpi-val" style="color: {'#3fb950' if curr_status=='HEALTHY' else ('#d2a8ff' if curr_status=='RECOVERED' else '#ff7b72')};">
            {curr_status}
        </div>
        <div class="kpi-sub">Gateway Concurrency Health</div>
    </div>
    """, unsafe_allow_html=True)

fail_pct = f"{metrics.failure_rate * 100:.1f}%" if metrics else "0.0%"
with k2:
    st.markdown(f"""
    <div class="kpi-card">
        <div class="kpi-label">HTTP Failure Rate</div>
        <div class="kpi-val" style="color: {'#3fb950' if (metrics and metrics.failure_rate == 0) else '#ff7b72'};">
            {fail_pct}
        </div>
        <div class="kpi-sub">503 Rejections / Saturated Slots</div>
    </div>
    """, unsafe_allow_html=True)

conns = f"{metrics.active_connections} / {metrics.max_connections}" if metrics else "0 / 50"
with k3:
    st.markdown(f"""
    <div class="kpi-card">
        <div class="kpi-label">Connection Pool</div>
        <div class="kpi-val">{conns}</div>
        <div class="kpi-sub">Active Slots / Capacity Limit</div>
    </div>
    """, unsafe_allow_html=True)

req_text = f"{metrics.successful_requests} ok / {metrics.rate_limited_503} err" if metrics else "0 / 0"
with k4:
    last_res = st.session_state.postfix_result or st.session_state.baseline_result
    verdict = f"{last_res.status} ({last_res.total_requests} reqs)" if last_res else "No verification run"
    st.markdown(f"""
    <div class="kpi-card">
        <div class="kpi-label">Verification Verdict</div>
        <div class="kpi-val" style="font-size: 19px; line-height: 1.5;">{verdict}</div>
        <div class="kpi-sub">Empirical Concurrency Test</div>
    </div>
    """, unsafe_allow_html=True)

st.markdown("<div style='height: 14px;'></div>", unsafe_allow_html=True)


# ==========================================
# INCIDENT LIFECYCLE WORKFLOW TOOLBAR
# ==========================================
st.markdown("#### ⚡ Incident Lifecycle Control Center")
btn_col1, btn_col2, btn_col3, btn_col4, btn_col5 = st.columns(5)

# Step 1: Inject Incident
with btn_col1:
    inject_disabled = not is_service_online
    if st.button("💥 1. Inject Incident", use_container_width=True, disabled=inject_disabled, help="Throttles connection pool to 2 slots and causes 503 errors under load."):
        with st.spinner("Injecting database connection pool starvation..."):
            import httpx
            payload = IncidentPayload(
                severity="HIGH",
                max_connections=2,
                processing_delay_ms=200.0,
                reason="Simulated upstream connection pool starvation (DB thread exhaustion)"
            )
            try:
                res = run_sync(httpx.AsyncClient().post(
                    f"{SERVICE_URL}/admin/inject-incident",
                    json=payload.model_dump(),
                    headers={"X-Warden-Admin-Token": ADMIN_TOKEN}
                ))
                log_incident_injected(payload.severity, payload.max_connections, payload.reason)
                st.session_state.incident_status = "DEGRADED"
                st.session_state.operator_approved = False
                st.session_state.remediation_applied = False
                st.session_state.postfix_result = None
                st.session_state.latest_diagnosis = None
                st.success("Incident injected: Pool restricted to 2 slots!")
                time.sleep(0.5)
                st.rerun()
            except Exception as e:
                st.error(f"Failed to inject incident: {e}")

# Step 2: Baseline Concurrency Load Test
with btn_col2:
    verify_disabled = not is_service_online
    if st.button("🧪 2. Run Verification", use_container_width=True, disabled=verify_disabled, help="Fires 30 concurrent requests to measure actual failure rates."):
        with st.spinner("Dispatching 30 concurrent requests to live service..."):
            verifier = ServiceVerifier(service_base_url=SERVICE_URL)
            phase_name = "post_remediation" if st.session_state.remediation_applied else "baseline"
            res = run_sync(verifier.run_verification(total_requests=30, concurrency=15, phase=phase_name))
            if st.session_state.remediation_applied:
                st.session_state.postfix_result = res
                is_rec, msg = verifier.is_recovered(st.session_state.baseline_result, res)
                if is_rec:
                    st.session_state.incident_status = "RECOVERED"
            else:
                st.session_state.baseline_result = res
            st.rerun()

# Step 3: Run AI Diagnostics
with btn_col3:
    diag_disabled = not is_service_online
    if st.button("🤖 3. AI Diagnostics", use_container_width=True, disabled=diag_disabled, help="Agent inspects telemetry and logs to hypothesize root cause and propose fix."):
        with st.spinner(f"Warden agent ({st.session_state.mode} mode) analyzing telemetry..."):
            agent = WardenAgent(service_base_url=SERVICE_URL)
            try:
                diagnosis = run_sync(agent.diagnose(
                    mode=st.session_state.mode,
                    reasoning_mode=st.session_state.reasoning_mode,
                    include_research=tavily_client.is_available()
                ))
                st.session_state.latest_diagnosis = diagnosis
                st.success("Root cause analyzed and remediation formulated!")
                st.rerun()
            except NebiusAPIError as err:
                st.error(f"Live AI Error: {err.message}")
                st.info(f"💡 Suggestion: {err.suggestion}")

# Step 4: Apply Approved Fix
with btn_col4:
    has_diag = st.session_state.latest_diagnosis is not None
    can_apply = has_diag and st.session_state.operator_approved and not st.session_state.remediation_applied
    if st.button("🛡️ 4. Apply Fix", use_container_width=True, disabled=not can_apply, help="Applies validated configuration change to running service."):
        with st.spinner("Applying safe configuration update..."):
            engine = RemediationEngine(service_base_url=SERVICE_URL)
            diag = st.session_state.latest_diagnosis
            try:
                success, msg, diff = run_sync(engine.apply_remediation(
                    action=diag.action,
                    parameters=diag.parameters,
                    is_approved=True
                ))
                st.session_state.remediation_applied = True
                st.session_state.last_diff = diff
                st.success(msg)
                time.sleep(0.5)
                st.rerun()
            except (RemediationSecurityError, RemediationValidationError) as e:
                st.error(f"Remediation blocked by safety engine: {e}")

# Step 5: Verify Recovery
with btn_col5:
    can_verify_rec = st.session_state.remediation_applied
    if st.button("✅ 5. Verify Recovery", use_container_width=True, disabled=not can_verify_rec, help="Re-tests service concurrency to verify 0.0% failure rate."):
        with st.spinner("Executing post-fix verification harness..."):
            verifier = ServiceVerifier(service_base_url=SERVICE_URL)
            res = run_sync(verifier.run_verification(total_requests=30, concurrency=15, phase="post_remediation"))
            st.session_state.postfix_result = res
            is_rec, msg = verifier.is_recovered(st.session_state.baseline_result, res)
            if is_rec:
                st.session_state.incident_status = "RECOVERED"
                st.balloons()
            st.rerun()

st.markdown("<div style='height: 18px;'></div>", unsafe_allow_html=True)


# ==========================================
# PANELS: TABS FOR EVIDENCE, REMEDIATION, VERIFICATION & LOGS
# ==========================================
tab_diag, tab_remed, tab_verif, tab_telemetry = st.tabs([
    "🤖 AI Diagnosis & Evidence",
    "🔧 Safe Remediation & Approval",
    "📊 Concurrency Verification & Recovery",
    "📜 Live Logs & Telemetry"
])

# ----------------------------------------------------
# TAB 1: AI DIAGNOSIS & EVIDENCE
# ----------------------------------------------------
with tab_diag:
    diag = st.session_state.latest_diagnosis
    if not diag:
        st.info("No diagnostic report generated yet. Click **'3. AI Diagnostics'** to inspect observed metrics and generate a root cause hypothesis.")
        if config and config.is_degraded:
            st.warning("⚠️ Active incident detected: Connection pool is restricted. Run AI Diagnostics to examine failure mechanisms.")
    else:
        # Display Mode and Model Banner
        col_m1, col_m2, col_m3 = st.columns([2, 1, 1])
        with col_m1:
            st.markdown(f"**Diagnostic Agent:** `{diag.reasoning_model or 'Warden Agent'}`")
        with col_m2:
            st.markdown(f"**Severity:** `{diag.severity}`")
        with col_m3:
            st.markdown(f"**Confidence:** `{diag.confidence * 100:.1f}%`")

        st.progress(diag.confidence)

        st.markdown("### 🔍 Root Cause Hypothesis")
        st.markdown(f"""
        <div style="background-color: #121826; border-left: 4px solid #58a6ff; padding: 14px; border-radius: 6px; margin: 10px 0;">
            <p style="margin: 0; font-size: 14px; color: #e6edf3;">{diag.root_cause_hypothesis}</p>
        </div>
        """, unsafe_allow_html=True)

        col_ev, col_plan = st.columns(2)
        with col_ev:
            st.markdown("#### 📋 Empirical Evidence")
            for idx, ev in enumerate(diag.evidence):
                st.markdown(f"- **[E{idx+1}]** {ev}")

        with col_plan:
            st.markdown("#### 🎯 Expected Outcome & Verification Plan")
            st.markdown(f"**Expected Outcome:** {diag.expected_outcome}")
            st.markdown(f"**Verification Plan:** {diag.verification_plan}")

        if diag.tavily_research:
            st.markdown("---")
            st.markdown("#### 🌐 Tavily Technical Research (Advisory Context)")
            for item in diag.tavily_research:
                with st.expander(f"📄 {item.get('title', 'External Reference')}"):
                    st.markdown(f"**URL:** [{item.get('url')}]({item.get('url')})")
                    st.markdown(f"> {item.get('snippet')}")

# ----------------------------------------------------
# TAB 2: SAFE REMEDIATION & OPERATOR APPROVAL
# ----------------------------------------------------
with tab_remed:
    diag = st.session_state.latest_diagnosis
    if not diag:
        st.info("Run AI Diagnostics first to inspect remediation proposals.")
    else:
        st.markdown("### 🛡️ Safe Remediation Proposal")
        st.markdown(f"""
        The agent proposed action: `RemediationAction.{diag.action.upper()}`.
        Parameters: `{json.dumps(diag.parameters)}`
        """)

        st.markdown(f"**Safety Guardrails Enforced:**")
        st.markdown("- ✅ Action is in allowlist: `[increase_pool_limit, reset_service_config, no_op]`")
        st.markdown("- ✅ Parameter range validation: `max_connections ∈ [5, 200]`")
        st.markdown("- 🚫 Arbitrary shell, python, and system calls are strictly blocked")

        st.markdown("---")
        st.markdown("#### 🔄 Configuration Diff (Before vs After)")
        
        diff = st.session_state.last_diff
        if diff:
            st.success("✅ Fix applied! Effective configuration diff:")
            diff_rows = []
            for k, v in diff.items():
                diff_rows.append({"Parameter": k, "Before Fix": v.get("before"), "After Fix (Remediated)": v.get("after")})
            st.table(pd.DataFrame(diff_rows))
        else:
            # Anticipated diff
            anticipated_pool = diag.parameters.get("max_connections", 50)
            st.table(pd.DataFrame([
                {
                    "Parameter": "max_connections",
                    "Current Value": config.max_connections if config else 2,
                    "Proposed Value": anticipated_pool
                },
                {
                    "Parameter": "is_degraded",
                    "Current Value": str(config.is_degraded if config else True),
                    "Proposed Value": "False"
                }
            ]))

        st.markdown("---")
        st.markdown("#### 👤 Human-in-the-Loop Operator Authorization")
        
        if not st.session_state.remediation_applied:
            approved = st.checkbox(
                "I have reviewed the diagnostic evidence, proposed configuration changes, and authorize Warden to apply this remediation.",
                value=st.session_state.operator_approved
            )
            st.session_state.operator_approved = approved
            if approved:
                log_remediation_approval(diag.action)
                st.success("Operator authorization granted. Click **'4. Apply Fix'** in the toolbar above to apply the change.")
        else:
            st.success("🎉 Remediation has been applied to the live service! Proceed to **'5. Verify Recovery'**.")

# ----------------------------------------------------
# TAB 3: CONCURRENCY VERIFICATION & RECOVERY
# ----------------------------------------------------
with tab_verif:
    st.markdown("### 📊 Concurrency Load Verification (Baseline vs Post-Fix)")
    
    base_res = st.session_state.baseline_result
    post_res = st.session_state.postfix_result

    if not base_res and not post_res:
        st.info("No verification tests run yet. Click **'2. Run Verification'** to send concurrent requests to the live payment service.")
    else:
        # Comparison Table
        records = []
        if base_res:
            records.append({
                "Phase": "Baseline (Pre-Fix)",
                "Total Reqs": base_res.total_requests,
                "HTTP 200 (Success)": base_res.success_200,
                "HTTP 503 (Dropped)": base_res.rate_limited_503,
                "Failure Rate": f"{base_res.failure_rate * 100:.1f}%",
                "Avg Latency (ms)": base_res.avg_latency_ms,
                "P95 Latency (ms)": base_res.p95_latency_ms,
                "Status": base_res.status
            })
        if post_res:
            records.append({
                "Phase": "Post-Remediation",
                "Total Reqs": post_res.total_requests,
                "HTTP 200 (Success)": post_res.success_200,
                "HTTP 503 (Dropped)": post_res.rate_limited_503,
                "Failure Rate": f"{post_res.failure_rate * 100:.1f}%",
                "Avg Latency (ms)": post_res.avg_latency_ms,
                "P95 Latency (ms)": post_res.p95_latency_ms,
                "Status": post_res.status
            })
        
        st.table(pd.DataFrame(records))

        # Recovery Verdict Banner
        if post_res:
            verifier = ServiceVerifier(service_base_url=SERVICE_URL)
            is_rec, msg = verifier.is_recovered(base_res, post_res)
            if is_rec:
                st.markdown(f"""
                <div style="background-color: rgba(46, 160, 67, 0.15); border: 2px solid #2ea043; border-radius: 8px; padding: 16px; margin-top: 15px;">
                    <h3 style="color: #3fb950; margin: 0 0 6px 0;">🎉 INCIDENT FULLY RECOVERED</h3>
                    <p style="margin: 0; color: #e6edf3; font-size: 14px;">{msg}</p>
                </div>
                """, unsafe_allow_html=True)
            else:
                st.markdown(f"""
                <div style="background-color: rgba(248, 81, 73, 0.15); border: 2px solid #f85149; border-radius: 8px; padding: 16px; margin-top: 15px;">
                    <h3 style="color: #ff7b72; margin: 0 0 6px 0;">⚠️ SERVICE STILL DEGRADED</h3>
                    <p style="margin: 0; color: #e6edf3; font-size: 14px;">{msg}</p>
                </div>
                """, unsafe_allow_html=True)

# ----------------------------------------------------
# TAB 4: LIVE SERVICE LOGS & TELEMETRY
# ----------------------------------------------------
with tab_telemetry:
    st.markdown("### 📜 Real-time Service Log Stream")
    if metrics and metrics.recent_logs:
        log_lines = []
        for l in metrics.recent_logs:
            ts = l.get("timestamp", "")[11:19]
            lvl = l.get("level", "INFO")
            msg = l.get("message", "")
            css_cls = "log-critical" if lvl == "CRITICAL" else ("log-warning" if lvl == "WARNING" else "log-info")
            log_lines.append(f'<span style="color: #8b949e;">[{ts}]</span> <span class="{css_cls}">[{lvl}]</span> {msg}')
        
        terminal_html = "<div class='terminal-box'>" + "<br>".join(log_lines) + "</div>"
        st.markdown(terminal_html, unsafe_allow_html=True)
    else:
        st.markdown("<div class='terminal-box'>[SYSTEM] Waiting for service logs...</div>", unsafe_allow_html=True)

    st.markdown("<div style='height: 15px;'></div>", unsafe_allow_html=True)
    col_raw1, col_raw2 = st.columns(2)
    with col_raw1:
        st.markdown("#### Live Metrics Payload (`/metrics`)")
        st.json(metrics.model_dump() if metrics else {})
    with col_raw2:
        st.markdown("#### Live Config Payload (`/config`)")
        st.json(config.model_dump() if config else {})


# ==========================================
# AUDIT TRAIL TIMELINE (FULL LIFECYCLE AUDIT)
# ==========================================
st.markdown("<hr style='border: none; border-top: 1px solid #212c42; margin: 30px 0 20px 0;'>", unsafe_allow_html=True)
st.markdown("### ⏱️ Immutable Audit Trail & Regulatory Timeline")
st.caption("Complete chronological record of all incident events, AI diagnostic hypotheses, operator approvals, and empirical verifications.")

events = get_audit_events()
if not events:
    st.markdown("<div style='color: #8b949e;'>No audit events recorded yet. Perform actions above to build timeline.</div>", unsafe_allow_html=True)
else:
    event_data = []
    for ev in reversed(events):
        event_data.append({
            "Timestamp (UTC)": ev.timestamp,
            "Event Type": ev.event_type,
            "Actor": ev.actor,
            "Summary": ev.summary,
            "Status": ev.status
        })
    st.dataframe(pd.DataFrame(event_data), use_container_width=True)

    with st.expander("📥 Export Raw Audit Trail (JSON)"):
        st.json([e.model_dump() for e in events])
