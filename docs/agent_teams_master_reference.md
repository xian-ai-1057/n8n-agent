# Agent Teams — Master Reference Guide

Source of truth: https://code.claude.com/docs/en/agent-teams
Minimum Claude Code version: **v2.1.32**
Status: **Experimental** — disabled by default.

This guide is the canonical reference used when building agent teams in this repo. It captures the mental model, the exact configuration knobs, the coordination primitives, the failure modes, and the team-design patterns that have been shown to work.

---

## 1. What an agent team actually is

An agent team is a set of **independent Claude Code sessions** that coordinate through a **shared task list** and a **direct messaging mailbox**. One session is the **lead**; the rest are **teammates**.

Key properties:

- Each teammate has its **own context window**. The lead's conversation history does **not** carry over into teammates.
- Teammates are spawned by the lead but run as full sessions: you can message them directly, not only through the lead.
- Teammates can message **each other** (not only the lead). This is the defining difference from subagents.
- Task-claim uses **file locking** to prevent race conditions.
- Task dependencies are tracked; a blocked task cannot be claimed until its dependencies complete.

### 1.1 Agent teams vs subagents — when to pick which

| Dimension | Subagents | Agent teams |
|---|---|---|
| Context | Own window, result returned to caller | Own window, fully independent |
| Communication | Reports back to main agent only | Teammates message each other directly |
| Coordination | Main agent orchestrates everything | Shared task list, self-claim |
| Token cost | Lower (results are summarized up) | Higher (each teammate is a full session) |
| Best for | Focused lookups/reviews where only the answer matters | Work that needs discussion, debate, or cross-talk |

**Rule of thumb:** If the workers only need to hand results *upward*, use subagents. If they need to hand results *sideways* — challenge each other, pass deliverables, or own adjacent files — use an agent team.

---

## 2. Enabling and starting a team

### 2.1 Enable the feature

Set the env var (project `settings.json` or shell env):

```json
{
  "env": {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"
  }
}
```

### 2.2 Start a team

Just describe the task and the roles in natural language from the lead session:

```text
I'm designing a CLI tool that helps developers track TODO comments across
their codebase. Create an agent team to explore this from different angles:
one teammate on UX, one on technical architecture, one playing devil's
advocate.
```

The lead will spawn teammates, populate the shared task list, and start coordinating. Claude will not spawn a team without confirmation; you can also let Claude *propose* a team when it judges the task fits.

### 2.3 Display modes

| Mode | Where teammates live | Requires |
|---|---|---|
| `in-process` | Inside the lead's terminal; cycle with Shift+Down | Nothing extra |
| `tmux` (split panes) | One pane per teammate | tmux **or** iTerm2 + `it2` CLI with Python API enabled |
| `auto` *(default)* | `tmux` if already inside a tmux session; otherwise `in-process` | — |

Persist the mode in `~/.claude.json`:

```json
{ "teammateMode": "in-process" }
```

Override per-session:

```bash
claude --teammate-mode in-process
```

**In-process controls:** `Shift+Down` cycles teammates (wraps back to lead after the last), `Enter` opens a teammate's session, `Escape` interrupts their current turn, `Ctrl+T` toggles the task list.

**Split-pane gotchas:** Not supported in VS Code's integrated terminal, Windows Terminal, or Ghostty. For iTerm2, the recommended entrypoint is `tmux -CC`.

---

## 3. Architecture

| Component | Role |
|---|---|
| **Lead** | Creates the team, spawns teammates, coordinates work. Fixed for the team's lifetime — cannot be transferred. |
| **Teammate** | Independent Claude Code session working on assigned tasks. |
| **Task list** | Shared list; states are `pending` → `in progress` → `completed`. Supports dependencies. |
| **Mailbox** | Delivers messages between agents automatically (no polling). |

State lives on disk:

- Team config: `~/.claude/teams/{team-name}/config.json`
- Task list: `~/.claude/tasks/{team-name}/`

**Do not hand-edit the team config.** It holds runtime state (session IDs, tmux pane IDs) and is overwritten on every update. There is **no project-level team config** — `.claude/teams/teams.json` in a repo is treated as a plain file.

The team config's `members` array (name / agent ID / agent type) is the directory teammates read to discover each other.

---

## 4. Control surface

Everything is driven from the lead in natural language. The primitives the lead exposes:

### 4.1 Spawn shape

- **Count and models**: "Create a team with 4 teammates to refactor these modules in parallel. Use Sonnet for each teammate."
- **Named roles**: give each teammate a predictable name in the spawn prompt so you can reference them later.
- **From a subagent definition**: "Spawn a teammate using the `security-reviewer` agent type…"

### 4.2 Subagent definitions as teammate templates

You can reuse any subagent definition (project / user / plugin / CLI scope) as a teammate template.

When used as a teammate:

- `tools` allowlist is **honored** (team coordination tools — `SendMessage`, task tools — are always available regardless).
- `model` is **honored**.
- The definition body is **appended** to the teammate's system prompt (not replaced).
- `skills` and `mcpServers` frontmatter are **ignored** — teammates load those from project/user settings instead.

### 4.3 Direct teammate interaction

Every teammate is a full session. Message it directly to redirect, add context, or ask follow-ups. In-process: cycle with Shift+Down and type. Split pane: click into the pane.

### 4.4 Task assignment

- **Lead assigns** — "Give the scraping task to the researcher."
- **Self-claim** — after finishing, a teammate picks the next unblocked unassigned task.
- **Dependencies** — unblocking is automatic when prerequisite tasks complete.
- **File lock** — prevents two teammates claiming the same task.

### 4.5 Plan-approval gating

For risky work, require the teammate to plan in read-only mode before implementing:

```text
Spawn an architect teammate to refactor the authentication module.
Require plan approval before they make any changes.
```

The teammate submits a plan, the lead approves or rejects with feedback, and it iterates. The lead decides **autonomously**, so bake approval criteria into the spawn prompt: *"only approve plans that include test coverage"*, *"reject plans that modify the database schema"*, etc.

### 4.6 Messaging

- `message` — to one teammate.
- `broadcast` — to all teammates. Cost scales with team size; use sparingly.

### 4.7 Shutdown and cleanup

- Graceful teammate shutdown: *"Ask the researcher teammate to shut down"*. The teammate can reject with an explanation.
- Team cleanup: *"Clean up the team"*. Must be run by the **lead**. Fails if any teammate is still running — shut them down first. Running cleanup from a teammate can leave resources inconsistent.

### 4.8 Permissions

All teammates **inherit the lead's permission mode at spawn**. If the lead was started with `--dangerously-skip-permissions`, every teammate gets it. Per-teammate modes can be changed **after** spawn but not **at** spawn time. Permission prompts from teammates bubble up to the lead — pre-approve common operations before spawning to avoid interruption storms.

### 4.9 Hooks — quality gates for team events

| Hook | Fires when | Exit code 2 behavior |
|---|---|---|
| `TeammateIdle` | Teammate is about to go idle | Sends feedback and keeps it working |
| `TaskCreated` | Task is being created | Blocks creation, sends feedback |
| `TaskCompleted` | Task is being marked complete | Blocks completion, sends feedback |

Use these to enforce "tests must pass before `TaskCompleted` succeeds" or "reject any `TaskCreated` that edits `schema/`".

---

## 5. Team-design playbook

### 5.1 Sizing

- **3–5 teammates** is the sweet spot for most workflows; the docs' examples cluster in this range.
- **5–6 tasks per teammate** keeps everyone productive without context-switch thrash.
- Token cost and coordination cost both scale roughly linearly with team size.
- Three focused teammates usually beat five scattered ones.

### 5.2 Task sizing

| Too small | Too large | Just right |
|---|---|---|
| Coordination > benefit | Teammates drift, wasted work before check-in | Self-contained deliverable: a function, a test file, a review |

If the lead doesn't decompose enough, say so: *"Split this into smaller tasks."*

### 5.3 Patterns that work

**Parallel code review (multi-lens):**

```text
Create an agent team to review PR #142. Spawn three reviewers:
- One focused on security implications
- One checking performance impact
- One validating test coverage
Have them each review and report findings.
```

Each lens is independent, so no serialization penalty. Lead synthesizes at the end.

**Adversarial investigation (competing hypotheses):**

```text
Users report the app exits after one message instead of staying connected.
Spawn 5 agent teammates to investigate different hypotheses. Have them talk
to each other to try to disprove each other's theories, like a scientific
debate. Update the findings doc with whatever consensus emerges.
```

The direct-messaging capability is the whole point here — a subagent swarm could not do this, because subagents can't talk sideways. Anchoring bias is fought by making disproof the explicit job.

**Cross-layer feature work:** one teammate per layer (frontend / backend / tests), each owning a disjoint file set.

### 5.4 Spawn-prompt template (copy this shape)

```text
Spawn a <role> teammate with the prompt:
"<Task statement>.
Focus on <scope>.
Context: <system facts the teammate needs — this does NOT inherit from the lead>.
Deliverable: <what done looks like>.
Report <severity/format requirements>."
```

Good example from the docs:

```text
Spawn a security reviewer teammate with the prompt: "Review the authentication
module at src/auth/ for security vulnerabilities. Focus on token handling,
session management, and input validation. The app uses JWT tokens stored in
httpOnly cookies. Report any issues with severity ratings."
```

### 5.5 Anti-patterns

- **Two teammates editing the same file.** Overwrites. Partition the file set.
- **Sequential-dependency work parallelized as a team.** Coordination overhead dominates. Use a single session or subagents.
- **Lead doing implementation instead of delegating.** Tell it: *"Wait for your teammates to complete their tasks before proceeding."*
- **Unattended long runs.** Monitor. Redirect when a teammate goes sideways — it won't self-correct well from errors.
- **Skipping spawn-prompt context.** Teammates don't inherit the lead's chat history. Under-briefed teammates are the #1 source of wasted tokens.

---

## 6. Troubleshooting quick table

| Symptom | First thing to try |
|---|---|
| Teammates "missing" after spawn | In-process: Shift+Down to cycle; Claude may not have spawned any if the task was small. |
| Split panes not opening | `which tmux`; for iTerm2 verify `it2` CLI + Python API enabled. |
| Permission prompt flood | Pre-approve common operations in `settings.json` before spawning. |
| Teammate stopped after error | Message it directly, or spawn a replacement; teammates don't robustly self-recover. |
| Lead declares done too early | Tell it to keep going / wait for teammates. |
| Task stuck in-progress | Teammates sometimes miss marking complete — update status manually or nudge via the lead. |
| Orphan tmux session | `tmux ls` then `tmux kill-session -t <name>`. |
| `/resume` brings back lead but no teammates | In-process teammates don't survive resume. Ask the lead to spawn new ones. |

---

## 7. Hard limitations to plan around

- **No session resumption for in-process teammates.** `/resume` and `/rewind` restore the lead only; the lead may try to message teammates that no longer exist.
- **Task status can lag.** Completion marking is not always reliable; have a nudging step in long-running flows.
- **Shutdown is not instant.** Teammates finish the current request/tool call first.
- **One team per session.** Clean up before starting another.
- **No nested teams.** Teammates cannot spawn their own teams or teammates.
- **Lead is fixed for life.** No promotion, no transfer of leadership.
- **Permissions only at spawn.** Change per-teammate only *after* spawn.
- **Split-pane terminal support is narrow.** Not VS Code integrated, not Windows Terminal, not Ghostty.

---

## 8. Checklist — spinning up a new team

1. `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` is set (check `settings.json` / env).
2. Claude Code is ≥ v2.1.32 (`claude --version`).
3. Decide display mode; if tmux, confirm it's installed.
4. Pre-approve frequent tool/bash operations to cut permission prompts.
5. Write the team-shape prompt: roles, count, models, subagent definitions to reuse.
6. For risky work: add "require plan approval" + the approval criteria.
7. Draft per-teammate spawn prompts with scope + context + deliverable — remember, no history carries over.
8. Partition the file set so no two teammates write to the same file.
9. Consider hooks: `TaskCompleted` to gate on tests, `TeammateIdle` to keep them working.
10. Monitor. Redirect early. When done, **ask the lead to clean up** (never a teammate).

---

## 9. Further reading

- Subagents: https://code.claude.com/docs/en/sub-agents
- Hooks: https://code.claude.com/docs/en/hooks (specifically `TeammateIdle`, `TaskCreated`, `TaskCompleted`)
- Settings: https://code.claude.com/docs/en/settings
- Token costs: https://code.claude.com/docs/en/costs#agent-team-token-costs
- Git worktrees for manual parallel sessions: https://code.claude.com/docs/en/common-workflows#run-parallel-claude-code-sessions-with-git-worktrees
- Feature comparison table: https://code.claude.com/docs/en/features-overview#compare-similar-features
