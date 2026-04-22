// Conservative App — orchestrates state and wires to the real FastAPI backend.
//
// Flow: submit(prompt) → POST /chat → render returned workflow_json + errors.
// During the (up to 180s) backend call we animate a fake staged progression
// through plan → build → assemble → validate → deploy so the user gets
// feedback. On response we drop back to done/error and show the result.

const { useState: useCS, useEffect: useCE, useRef: useCR } = React;

function nowStamp() {
  const d = new Date();
  const pad = n => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function useBackendRunner() {
  const [phase, setPhase] = useCS("empty"); // empty | generating | done | error
  const [currentStage, setCurrentStage] = useCS(null);
  const [buildingUpTo, setBuildingUpTo] = useCS(null);
  const [messages, setMessages] = useCS([]);
  const [errors, setErrors] = useCS([]);
  const [workflow, setWorkflow] = useCS(null);
  const [view, setView] = useCS("diagram");
  const [highlightedNode, setHighlightedNode] = useCS(null);
  const [deployedUrl, setDeployedUrl] = useCS(null);
  const [history, setHistory] = useCS([]);     // [{id, title, time, ok}]
  const [deploys, setDeploys] = useCS([]);     // [{id, name, nodes, status, lastRun, url}]

  const timers = useCR([]);
  const abortRef = useCR(null);

  const clearTimers = () => { timers.current.forEach(clearTimeout); timers.current = []; };

  function addMsg(m) {
    setMessages(prev => [...prev, { id: Math.random().toString(36).slice(2, 7), ...m }]);
  }
  function updateLastMsg(patch) {
    setMessages(prev => {
      if (!prev.length) return prev;
      const copy = prev.slice();
      copy[copy.length - 1] = { ...copy[copy.length - 1], ...patch };
      return copy;
    });
  }

  function reset() {
    clearTimers();
    if (abortRef.current) { try { abortRef.current.abort(); } catch(e){} abortRef.current = null; }
    setPhase("empty");
    setCurrentStage(null);
    setBuildingUpTo(null);
    setMessages([]);
    setErrors([]);
    setWorkflow(null);
    setHighlightedNode(null);
    setDeployedUrl(null);
    setView("diagram");
  }

  // Kick off the fake staged progress while the real request is in flight.
  // Stages are timed roughly to fit the backend's ~180s budget, but they
  // loop at "validate" if the request hasn't returned yet.
  function scheduleProgress() {
    clearTimers();

    timers.current.push(setTimeout(() => {
      setCurrentStage("plan");
      updateLastMsg({ currentStage: "plan", thinkingLabel: "規劃步驟中" });
    }, 200));

    timers.current.push(setTimeout(() => {
      setCurrentStage("build");
      updateLastMsg({ currentStage: "build", thinkingLabel: "從 529 個節點檢索並建置" });
    }, 6_000));

    timers.current.push(setTimeout(() => {
      setCurrentStage("assemble");
      updateLastMsg({ currentStage: "assemble", thinkingLabel: "連接節點" });
    }, 30_000));

    timers.current.push(setTimeout(() => {
      setCurrentStage("validate");
      updateLastMsg({ currentStage: "validate", thinkingLabel: "執行 validator" });
    }, 60_000));

    timers.current.push(setTimeout(() => {
      setCurrentStage("deploy");
      updateLastMsg({ currentStage: "deploy", thinkingLabel: "部署到 n8n" });
    }, 120_000));
  }

  async function submit(prompt) {
    const userContent = (prompt || "").trim();
    if (!userContent) return;
    // Abort any in-flight request before starting a new one
    if (abortRef.current) { try { abortRef.current.abort(); } catch(e){} }
    const ac = new AbortController();
    abortRef.current = ac;

    // Backend `/chat` is stateless — it takes a single `message` and runs the
    // full plan→build→…→deploy graph from scratch. To preserve multi-turn
    // intent we prepend prior user messages as context so the agent sees the
    // whole history, not just the latest refinement.
    const priorUserMsgs = messages
      .filter(m => m.role === "user")
      .map(m => m.content);
    const effectivePrompt =
      priorUserMsgs.length === 0
        ? userContent
        : [
            "原始需求:",
            priorUserMsgs[0],
            priorUserMsgs.length > 1 ? "\n後續調整:" : "",
            ...priorUserMsgs.slice(1).map(p => `- ${p}`),
            "\n當前指令:",
            userContent,
          ].filter(Boolean).join("\n");

    clearTimers();
    addMsg({ role: "user", content: userContent });
    addMsg({
      role: "assistant",
      pipelineBar: true,
      currentStage: "plan",
      thinking: true,
      thinkingLabel: "規劃步驟中",
    });
    setPhase("generating");
    setCurrentStage("plan");
    setErrors([]);
    setWorkflow(null);
    setDeployedUrl(null);
    setBuildingUpTo(null);
    scheduleProgress();

    const started = performance.now();
    try {
      const { status, data } = await postChat(effectivePrompt, { signal: ac.signal });
      const elapsedS = (performance.now() - started) / 1000;
      clearTimers();
      abortRef.current = null;

      const wfRaw = data.workflow_json || null;
      const wf = wfRaw ? normalizeWorkflow(wfRaw) : null;
      setWorkflow(wf);

      const errs = Array.isArray(data.errors) ? data.errors : [];
      setErrors(errs);

      const deployed = !!data.workflow_url;
      setDeployedUrl(data.workflow_url || null);

      // Done path ---------------------------------------------
      if (data.ok) {
        setPhase("done");
        setCurrentStage("done");
        setBuildingUpTo(null);
        setView("diagram");
        updateLastMsg({
          thinking: false,
          currentStage: "done",
          content: deployed
            ? "完成!已部署到你的本機 n8n,點「開啟 workflow」即可編輯。"
            : "完成!Workflow JSON 已生成 (未部署,因為 N8N_API_KEY 未設定)。",
          workflowRef: wf ? {
            name: wf.name,
            id: data.workflow_id,
            url: data.workflow_url,
            nodeCount: wf.nodes.length,
            elapsedS,
            retryCount: data.retry_count,
            deployed,
          } : null,
        });
        if (wf) {
          pushHistory({ title: wf.name, ok: true });
          if (deployed) {
            pushDeploy({
              id: data.workflow_id || Math.random().toString(36).slice(2, 8),
              name: wf.name,
              nodes: wf.nodes.length,
              status: "active",
              lastRun: "剛剛",
              url: data.workflow_url,
            });
          }
        }
        return;
      }

      // Validator failed path ---------------------------------
      if (errs.length > 0) {
        setPhase("error");
        setCurrentStage("validate");
        setView("errors");
        updateLastMsg({
          thinking: false,
          currentStage: "validate",
          content: `Validator 回報 ${errs.length} 個問題 (重試 ${data.retry_count || 0} 次),已列在右邊面板。`,
          errorBanner: data.error_message || null,
        });
        if (wf) pushHistory({ title: wf.name, ok: false });
        return;
      }

      // Other errors ------------------------------------------
      setPhase("error");
      updateLastMsg({
        thinking: false,
        content: data.error_message || `後端錯誤 HTTP ${status}`,
        errorBanner: data.error_message || `HTTP ${status}`,
      });
    } catch (err) {
      clearTimers();
      abortRef.current = null;
      const aborted = err?.name === "AbortError";
      setPhase("error");
      updateLastMsg({
        thinking: false,
        content: aborted
          ? "已取消。"
          : "連不到後端或請求逾時:" + (err?.message || String(err)),
        errorBanner: aborted ? null : (err?.message || String(err)),
      });
    }
  }

  function stop() {
    if (abortRef.current) { try { abortRef.current.abort(); } catch(e){} }
  }

  function pushHistory(item) {
    const id = "h-" + Math.random().toString(36).slice(2, 7);
    setHistory(prev => [{ id, title: item.title, time: nowStamp(), ok: !!item.ok }, ...prev].slice(0, 20));
  }
  function pushDeploy(item) {
    setDeploys(prev => [item, ...prev].slice(0, 10));
  }

  useCE(() => () => clearTimers(), []);

  return {
    phase, currentStage, buildingUpTo,
    messages, errors, workflow, view, setView,
    highlightedNode, setHighlightedNode,
    deployedUrl,
    history, deploys,
    submit, stop, reset,
  };
}

function ConservativeApp({ density, theme, onDensityChange, onThemeChange }) {
  const demo = useBackendRunner();
  const scrollRef = useCR(null);

  const [backendUrl, setBackendUrlState] = useCS(() => getBackendUrl());
  const [health, setHealth] = useCS({
    openai: { ok: false, detail: "尚未檢查" },
    n8n:    { ok: false, detail: "尚未檢查" },
    chroma: { ok: false, detail: "尚未檢查" },
    ok: false,
  });

  async function refreshHealth() {
    try {
      const raw = await getHealth();
      setHealth(toHealthRows(raw));
    } catch (err) {
      setHealth({
        openai: { ok: false, detail: "連線失敗" },
        n8n:    { ok: false, detail: "連線失敗" },
        chroma: { ok: false, detail: "連線失敗" },
        ok: false,
      });
    }
  }

  useCE(() => {
    refreshHealth();
    const iv = setInterval(refreshHealth, 30_000);
    return () => clearInterval(iv);
  }, [backendUrl]);

  useCE(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [demo.messages.length, demo.messages[demo.messages.length - 1]?.content]);

  const hasConversation = demo.messages.length > 0;

  return (
    <div className={"cv-app cv-app--" + density}>
      <ConservativeSidebar
        density={density}
        theme={theme}
        onThemeToggle={() => onThemeChange(theme === "dark" ? "light" : "dark")}
        onDensityToggle={(v) => onDensityChange(v)}
        onNewChat={demo.reset}
        health={health}
        onRefreshHealth={refreshHealth}
        history={demo.history}
        activeHistoryId={null}
        onSelectHistory={() => {}}
        deploys={demo.deploys}
        backendUrl={backendUrl}
        onBackendUrlChange={(url) => {
          setBackendUrl(url);
          setBackendUrlState(url);
        }}
      />

      <main className="cv-main">
        <div className="cv-main__head">
          <div className="cv-main__crumbs">
            <span>對話</span>
            <Icon name="arrow" size={13}/>
            <span className="cv-main__crumb-cur">
              {hasConversation
                ? (demo.workflow?.name || "生成中…")
                : "新對話"}
            </span>
          </div>
          <div className="cv-main__head-right">
            {demo.phase === "generating" && (
              <PipelineTrack currentStage={demo.currentStage} stages={PIPELINE_STAGES} compact />
            )}
          </div>
        </div>

        <div className="cv-main__body">
          <div className="cv-conv" ref={scrollRef}>
            {!hasConversation ? (
              <ConservativeEmpty
                onSubmit={(v) => demo.submit(v)}
                onSelectPrompt={(t) => demo.submit(t)}
              />
            ) : (
              <div className="cv-conv__list">
                {demo.messages.map((m) => (
                  <ConservativeMessage key={m.id} msg={m} />
                ))}
              </div>
            )}
          </div>

          {hasConversation && (
            <ConservativeComposer
              onSubmit={(v) => demo.submit(v)}
              disabled={demo.phase === "generating"}
              onStop={demo.stop}
            />
          )}
        </div>
      </main>

      <WorkflowPreview
        workflow={demo.workflow}
        view={demo.view}
        onViewChange={demo.setView}
        errors={demo.errors}
        phase={demo.phase}
        buildingUpTo={demo.buildingUpTo}
        highlightedNode={demo.highlightedNode}
        setHighlightedNode={demo.setHighlightedNode}
        deployedUrl={demo.deployedUrl}
      />
    </div>
  );
}

window.ConservativeApp = ConservativeApp;
