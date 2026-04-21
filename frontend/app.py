"""Streamlit UI (Implements C1-6).

Run locally::

    cd frontend
    /opt/miniconda3/envs/agent/bin/streamlit run app.py --server.port 8501

The page talks to the FastAPI backend via ``POST {backend_url}/chat``. It
keeps the full conversation in ``st.session_state.messages``.
"""

from __future__ import annotations

import json
import os
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

if "messages" not in st.session_state:
    st.session_state.messages = []  # list[dict]
if "last_error" not in st.session_state:
    st.session_state.last_error = None


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
                    "Ollama", "OK" if data.get("ollama") else "FAIL",
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
    if st.button("清空對話歷史", use_container_width=True):
        st.session_state.messages = []
        st.session_state.last_error = None
        st.rerun()

    st.caption(
        "介面語言：zh-Hant。/chat 最長等待 180 秒（backend 同步呼叫 LangGraph）。"
    )


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _render_assistant(msg: dict[str, Any], n8n_base: str) -> None:
    content = msg.get("content") or ""
    if content:
        st.markdown(content)

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


# ----------------------------------------------------------------------
# main: history + input
# ----------------------------------------------------------------------

st.subheader("對話")

if st.session_state.last_error:
    st.error(st.session_state.last_error)

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        if msg["role"] == "user":
            st.markdown(msg["content"])
        else:
            _render_assistant(msg, n8n_url)

prompt = st.chat_input("描述你想要的 workflow，例如：每小時抓 https://api.github.com/zen 存到 Google Sheet")

if prompt:
    st.session_state.last_error = None
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        status = st.status("生成 workflow 中… plan → build → validate → deploy", expanded=True)
        import time as _time

        t0 = _time.monotonic()
        data: dict[str, Any] | None = None
        error_text: str | None = None
        try:
            with httpx.Client(timeout=CHAT_TIMEOUT_S) as c:
                r = c.post(
                    f"{backend_url.rstrip('/')}/chat",
                    json={"message": prompt},
                )
            elapsed = _time.monotonic() - t0
            if r.status_code >= 500:
                error_text = f"後端錯誤（{r.status_code}）：{r.text[:300]}"
                status.update(label=f"失敗 (HTTP {r.status_code})", state="error")
            else:
                try:
                    data = r.json()
                except Exception:
                    error_text = f"無法解析回應：{r.text[:500]}"
                    status.update(label="回應格式錯誤", state="error")
                else:
                    ok = bool(data.get("ok"))
                    if ok:
                        status.update(label=f"完成 ({elapsed:.1f}s)", state="complete")
                    elif data.get("errors"):
                        status.update(
                            label=f"Validator 失敗（重試 {data.get('retry_count', 0)} 次）",
                            state="error",
                        )
                    else:
                        status.update(
                            label=data.get("error_message") or "失敗",
                            state="error",
                        )
        except httpx.TimeoutException:
            error_text = "生成超時（>200s），請再試"
            status.update(label="timeout", state="error")
        except httpx.RequestError as exc:
            error_text = f"無法連到 backend {backend_url}: {exc}"
            status.update(label="連線失敗", state="error")
        except Exception as exc:  # noqa: BLE001
            error_text = f"未預期錯誤：{exc}"
            status.update(label="錯誤", state="error")

        if error_text and data is None:
            st.error(error_text)
            st.session_state.last_error = error_text
            st.session_state.messages.append(
                {"role": "assistant", "content": error_text, "errors": []}
            )
        elif data is not None:
            content_bits = []
            if data.get("ok"):
                content_bits.append("Workflow 生成並部署成功。")
            elif data.get("workflow_json"):
                content_bits.append(
                    "Workflow 已生成，但未部署（缺 API key 或 validator 失敗）。"
                )
            else:
                content_bits.append(data.get("error_message") or "失敗。")
            elapsed = _time.monotonic() - t0
            assistant_msg = {
                "role": "assistant",
                "content": " ".join(content_bits),
                "workflow_url": data.get("workflow_url"),
                "workflow_id": data.get("workflow_id"),
                "workflow_json": data.get("workflow_json"),
                "retry_count": data.get("retry_count", 0),
                "errors": data.get("errors") or [],
                "elapsed_s": elapsed,
            }
            st.session_state.messages.append(assistant_msg)
            _render_assistant(assistant_msg, n8n_url)
