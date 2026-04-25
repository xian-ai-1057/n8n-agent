"""Streamlit UI (Implements C1-6).

Run locally::

    cd frontend
    /opt/miniconda3/envs/agent/bin/streamlit run app.py --server.port 8501

The page talks to the FastAPI backend via ``POST {backend_url}/chat``. It
keeps the full conversation in ``st.session_state.messages`` and tracks the
ongoing chat session in ``st.session_state.session_id``.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

DEFAULT_BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
DEFAULT_N8N_URL = os.environ.get("N8N_URL", "http://localhost:5678")

CHAT_TIMEOUT_S = 200  # backend budget is 180; leave a small margin


# ----------------------------------------------------------------------
# page setup
# ----------------------------------------------------------------------

st.set_page_config(page_title="n8n Workflow Builder", page_icon=":robot_face:", layout="wide")

# C1-6:CHAT-UI-01 — session state initialisation
if "messages" not in st.session_state:
    st.session_state.messages = []  # list[dict]
if "last_error" not in st.session_state:
    st.session_state.last_error = None
if "session_id" not in st.session_state:
    # C1-6:CHAT-UI-01 — starts empty; populated after first server response
    st.session_state.session_id = None
# C1-6:CHAT-UI-03 — tracks whether the last assistant turn is awaiting approval
if "awaiting_plan_approval" not in st.session_state:
    st.session_state.awaiting_plan_approval = False
# C1-6:CHAT-UI-03 — cached plan list from the last awaiting_plan_approval response
if "pending_plan" not in st.session_state:
    st.session_state.pending_plan = []
# C1-6:CHAT-UI-03 — signals that a plan action button was clicked and what message to send
if "plan_action_message" not in st.session_state:
    st.session_state.plan_action_message = None
# C1-6:CHAT-UI-03 — controls visibility of the edit text area
if "show_edit_area" not in st.session_state:
    st.session_state.show_edit_area = False
# P2-14 — guard: True immediately after user clicks a plan-action button.
# Prevents re-rendering the plan card when the very next response happens to
# carry status="awaiting_plan_approval" (e.g. a second confirm-plan round).
if "just_sent_plan_action" not in st.session_state:
    st.session_state.just_sent_plan_action = False


# ----------------------------------------------------------------------
# sidebar
# ----------------------------------------------------------------------

with st.sidebar:
    st.title("n8n Workflow Builder")
    backend_url = st.text_input("Backend URL", value=DEFAULT_BACKEND_URL)
    n8n_url = st.text_input("n8n UI URL", value=DEFAULT_N8N_URL)

    if st.button("檢查後端健康 /health", use_container_width=True):
        try:
            with httpx.Client(timeout=10) as c:
                r = c.get(f"{backend_url.rstrip('/')}/health")
            if r.status_code == 200:
                data = r.json()
                st.session_state.last_error = None
                c1, c2, c3 = st.columns(3)
                c1.metric(
                    "OpenAI", "OK" if data.get("openai") else "FAIL",
                    delta=None, delta_color="off",
                )
                c2.metric(
                    "n8n", "OK" if data.get("n8n") else "FAIL",
                    delta=None, delta_color="off",
                )
                c3.metric(
                    "Chroma", "OK" if data.get("chroma") else "FAIL",
                    delta=None, delta_color="off",
                )
                with st.expander("完整 /health JSON", expanded=False):
                    st.code(json.dumps(data, ensure_ascii=False, indent=2), language="json")
            else:
                st.error(f"health check HTTP {r.status_code}: {r.text[:200]}")
        except Exception as exc:  # noqa: BLE001
            st.error(f"無法連到 backend {backend_url}: {exc}")

    st.divider()

    # C1-6:CHAT-UI-01 — clear conversation also resets session_id (new session)
    if st.button("清空對話歷史", use_container_width=True):
        st.session_state.messages = []
        st.session_state.last_error = None
        st.session_state.session_id = None
        st.session_state.awaiting_plan_approval = False
        st.session_state.pending_plan = []
        st.session_state.plan_action_message = None
        st.session_state.show_edit_area = False
        st.session_state.just_sent_plan_action = False
        st.rerun()

    # C1-6:CHAT-UI-01 — debug session_id inspector (collapsed by default)
    with st.expander("Debug: Session ID", expanded=False):
        sid = st.session_state.session_id
        if sid:
            st.code(sid, language=None)
            if st.button("複製 session_id", use_container_width=True):
                st.write(f"`{sid}`")
                st.toast("Session ID 已顯示，請手動複製")
        else:
            st.caption("（尚未建立 session）")

    st.caption(
        "介面語言：zh-Hant。/chat 最長等待 180 秒（backend 同步呼叫 LangGraph）。"
    )


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


# C1-6:CHAT-UI-02 — render assistant message with new response shape
def _render_assistant(msg: dict[str, Any], n8n_base: str) -> None:
    # assistant_text is the primary content from chat LLM (C1-6:CHAT-UI-02)
    assistant_text = msg.get("assistant_text") or msg.get("content") or ""
    if assistant_text:
        st.markdown(assistant_text)

    # C1-6:CHAT-UI-02 — status="error" shows st.error; chat continues
    msg_status = msg.get("status", "chat")
    if msg_status == "error":
        err = msg.get("error_message") or "發生錯誤"
        st.error(err)

    # C1-6:CHAT-UI-02 — workflow_url / workflow_id only shown when status="completed" or "deployed"
    if msg_status in ("completed", "deployed"):
        url = msg.get("workflow_url")
        workflow_id = msg.get("workflow_id")
        if url:
            st.link_button("在 n8n 開啟 →", url=url, type="primary")
        elif workflow_id:
            st.link_button(
                "在 n8n 開啟 →",
                url=f"{n8n_base.rstrip('/')}/workflow/{workflow_id}",
                type="primary",
            )

        wf = msg.get("workflow_json")
        if wf:
            with st.expander("Workflow JSON", expanded=False):
                st.code(json.dumps(wf, ensure_ascii=False, indent=2), language="json")

    errors = msg.get("errors") or []
    if errors:
        with st.expander(f"Validator 錯誤（{len(errors)}）", expanded=False):
            for e in errors:
                rid = e.get("rule_id", "?")
                path = e.get("path", "")
                node = e.get("node_name", "")
                st.markdown(f"- **{rid}** [{node}] `{path}`  \n  {e.get('message')}")

    # C1-6:CHAT-UI-02 — tool_calls expander for debug observability
    tool_calls = msg.get("tool_calls") or []
    if tool_calls:
        with st.expander(f"Tool calls ({len(tool_calls)})", expanded=False):
            for tc in tool_calls:
                name = tc.get("name") or tc.get("tool_name", "unknown")
                args_summary = tc.get("args_summary") or tc.get("args") or {}
                result_status = tc.get("result_status") or tc.get("status", "")
                latency = tc.get("latency_ms")
                header = f"**{name}**"
                if result_status:
                    header += f"  →  `{result_status}`"
                if latency is not None:
                    header += f"  ({latency}ms)"
                st.markdown(header)
                if args_summary:
                    summary_str = (
                        json.dumps(args_summary, ensure_ascii=False, indent=2)
                        if isinstance(args_summary, dict)
                        else str(args_summary)
                    )
                    st.code(summary_str, language="json")

    retry_count = msg.get("retry_count", 0)
    elapsed = msg.get("elapsed_s")
    plan = msg.get("plan")
    meta_bits = []
    if retry_count:
        meta_bits.append(f"retries={retry_count}")
    if elapsed is not None:
        meta_bits.append(f"elapsed={elapsed:.1f}s")
    if meta_bits:
        with st.expander("執行摘要 / " + " · ".join(meta_bits), expanded=False):
            if plan:
                st.markdown("**Plan**")
                for step in plan:
                    st.markdown(f"- `{step.get('step_id','?')}` {step.get('description','')}")


# C1-6:CHAT-UI-03 — plan approval card rendered below the latest assistant message
def _render_plan_card(plan: list[dict[str, Any]]) -> str | None:
    """Render a bordered plan card with three action buttons.

    Returns the message string to send when a button is clicked, or None.
    """
    with st.container(border=True):
        st.markdown("### 確認執行計畫")
        if plan:
            for step in plan:
                step_id = step.get("step_id", "?")
                desc = step.get("description", "")
                cands = step.get("candidate_node_types") or []
                cand_str = ", ".join(f"`{c}`" for c in cands) if cands else ""
                line = f"**{step_id}**: {desc}"
                if cand_str:
                    line += f"  \n  候選節點: {cand_str}"
                st.markdown(line)
        else:
            st.caption("（無詳細計畫資料）")

        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("確認執行", key="plan_confirm", type="primary", use_container_width=True):
                return "確認執行"
        with col2:
            if st.button("我要修改", key="plan_edit", use_container_width=True):
                st.session_state.show_edit_area = True
        with col3:
            if st.button("取消", key="plan_cancel", use_container_width=True):
                return "我不想建立這個 workflow"

        # C1-6:CHAT-UI-03 — edit text area shown when "我要修改" is clicked
        if st.session_state.show_edit_area:
            edit_text = st.text_area(
                "請輸入修改說明",
                key="plan_edit_text",
                placeholder="例如：把步驟 2 改成用 Slack 通知",
            )
            if st.button("送出修改", key="plan_edit_submit", type="primary"):
                if edit_text and edit_text.strip():
                    st.session_state.show_edit_area = False
                    return edit_text.strip()
                else:
                    st.warning("請輸入修改說明後再送出")

    return None


# C1-6:CHAT-UI-02 / CHAT-UI-03 — send a message to backend and update state
def _send_message(prompt: str, backend: str, n8n_base: str) -> None:
    st.session_state.last_error = None
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        status_widget = st.status("處理中…", expanded=True)
        t0 = time.monotonic()
        data: dict[str, Any] | None = None
        error_text: str | None = None

        # C1-6:CHAT-UI-01 — include session_id in every request if we have one
        request_body: dict[str, Any] = {"message": prompt}
        if st.session_state.session_id:
            request_body["session_id"] = st.session_state.session_id

        try:
            with httpx.Client(timeout=CHAT_TIMEOUT_S) as c:
                r = c.post(
                    f"{backend.rstrip('/')}/chat",
                    json=request_body,
                )
            elapsed = time.monotonic() - t0

            # C1-6:CHAT-UI-02 — error handling per status code
            if r.status_code == 504:
                # Request timeout — keep session_id (C1-6:CHAT-UI-02)
                error_text = "處理逾時，請重試"
                status_widget.update(label="逾時 (504)", state="error")
            elif r.status_code == 404:
                # Session expired (C1-6:CHAT-UI-02)
                error_text = "session 已過期，將開新 session"
                st.session_state.session_id = None
                status_widget.update(label="session 過期 (404)", state="error")
            elif r.status_code == 400:
                # Bad request — message too long etc. (C1-6:CHAT-UI-02)
                try:
                    body = r.json()
                    error_text = body.get("error_message") or body.get("error") or f"請求錯誤（400）：{r.text[:200]}"
                except Exception:
                    error_text = f"請求錯誤（400）：{r.text[:200]}"
                status_widget.update(label="請求錯誤 (400)", state="error")
            elif r.status_code >= 500:
                error_text = f"後端錯誤（{r.status_code}）：{r.text[:300]}"
                status_widget.update(label=f"失敗 (HTTP {r.status_code})", state="error")
            else:
                try:
                    data = r.json()
                except Exception:
                    error_text = f"無法解析回應：{r.text[:500]}"
                    status_widget.update(label="回應格式錯誤", state="error")
                else:
                    resp_status = data.get("status", "chat")
                    if resp_status == "error":
                        status_widget.update(
                            label=data.get("error_message") or "失敗",
                            state="error",
                        )
                    elif resp_status in ("completed", "deployed"):
                        status_widget.update(label=f"完成 ({elapsed:.1f}s)", state="complete")
                    elif resp_status == "awaiting_plan_approval":
                        status_widget.update(label="等待確認計畫", state="complete")
                    elif resp_status == "rejected":
                        status_widget.update(label="計畫已取消", state="complete")
                    else:
                        # "chat" or unknown
                        status_widget.update(label=f"完成 ({elapsed:.1f}s)", state="complete")

        except httpx.TimeoutException:
            # C1-6:CHAT-UI-02 — 504 equivalent; session_id preserved
            error_text = "處理逾時（>200s），請再試"
            status_widget.update(label="timeout", state="error")
        except httpx.RequestError as exc:
            error_text = f"無法連到 backend {backend}: {exc}"
            status_widget.update(label="連線失敗", state="error")
        except Exception as exc:  # noqa: BLE001
            error_text = f"未預期錯誤：{exc}"
            status_widget.update(label="錯誤", state="error")

        if error_text and data is None:
            st.error(error_text)
            st.session_state.last_error = error_text
            st.session_state.messages.append(
                {"role": "assistant", "content": error_text, "errors": [], "status": "error"}
            )
        elif data is not None:
            elapsed = time.monotonic() - t0
            resp_status = data.get("status", "chat")

            # C1-6:CHAT-UI-01 — capture session_id from response
            returned_sid = data.get("session_id")
            if returned_sid:
                st.session_state.session_id = returned_sid

            # C1-6:CHAT-UI-02 — build assistant message using new shape
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                # assistant_text is primary; fall back to legacy content for compat
                "assistant_text": data.get("assistant_text") or "",
                "content": data.get("assistant_text") or "",
                "status": resp_status,
                "workflow_url": data.get("workflow_url"),
                "workflow_id": data.get("workflow_id"),
                "workflow_json": data.get("workflow_json"),
                "retry_count": data.get("retry_count", 0),
                "errors": data.get("errors") or [],
                "plan": data.get("plan") or [],  # C1-5:A-RESP-01
                "tool_calls": data.get("tool_calls") or [],  # C1-6:CHAT-UI-02
                "error_message": data.get("error_message"),
                "elapsed_s": elapsed,
            }
            st.session_state.messages.append(assistant_msg)
            _render_assistant(assistant_msg, n8n_base)

            # C1-6:CHAT-UI-03 — track plan approval state
            if resp_status == "awaiting_plan_approval":
                st.session_state.awaiting_plan_approval = True
                st.session_state.pending_plan = data.get("plan") or []
            else:
                st.session_state.awaiting_plan_approval = False
                st.session_state.pending_plan = []


# ----------------------------------------------------------------------
# main: history + input
# ----------------------------------------------------------------------

st.subheader("對話")

if st.session_state.last_error:
    st.error(st.session_state.last_error)

# Render existing chat history
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        if msg["role"] == "user":
            st.markdown(msg["content"])
        else:
            _render_assistant(msg, n8n_url)

# C1-6:CHAT-UI-03 — plan card is shown after the last assistant message if awaiting approval
# It must be outside the history loop so buttons get fresh keys each render.
# P2-14: suppress the card if user just clicked a plan-action button; the flag is
# reset after the plan action message is consumed so future rounds work normally.
if (
    st.session_state.awaiting_plan_approval
    and st.session_state.pending_plan
    and not st.session_state.just_sent_plan_action
):
    action_msg = _render_plan_card(st.session_state.pending_plan)
    if action_msg is not None:
        # Store the message to send; rerun so the chat_input area is not
        # competing with this render cycle.
        st.session_state.plan_action_message = action_msg
        st.session_state.awaiting_plan_approval = False
        st.session_state.show_edit_area = False
        # P2-14: mark that we just sent a plan action so the next render
        # does not show the plan card even if status="awaiting_plan_approval"
        st.session_state.just_sent_plan_action = True
        st.rerun()

# C1-6:CHAT-UI-03 — process any pending plan action message after rerun
if st.session_state.plan_action_message is not None:
    action = st.session_state.plan_action_message
    st.session_state.plan_action_message = None
    # P2-14: reset the guard so subsequent awaiting_plan_approval responses
    # are rendered correctly.
    st.session_state.just_sent_plan_action = False
    _send_message(action, backend_url, n8n_url)
    st.rerun()

# C1-6:CHAT-UI-02 — main chat input
prompt = st.chat_input("描述你想要的 workflow，例如：每小時抓 https://api.github.com/zen 存到 Google Sheet")

if prompt:
    _send_message(prompt, backend_url, n8n_url)
    st.rerun()
