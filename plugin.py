"""Ralph Loop — hat-based multi-agent orchestration for lele.

Port of pi-ralph (samfoy/pi-ralph). Keeps the agent iterating through
specialized hats until a task is complete. Each hat has its own instructions,
triggers, and events that drive the workflow forward.

Commands:
  /ralph <preset> <prompt>  — start a loop
  /ralph stop               — stop the current loop
  /ralph status             — show loop status
  /ralph pause              — pause the current loop
  /ralph resume             — resume a paused loop
  /ralph steer <msg>        — inject guidance into the current hat
  /ralph history            — show iteration history
  /ralph loops              — browse past loop records
  /ralph presets            — list available presets
  /plan <idea>              — start a PDD planning session

Tools:
  start_ralph_loop({preset, prompt}) — LLM-callable loop start

Presets loaded from (project overrides user overrides built-in):
  <plugin>/presets/*.yml          (built-in)
  .lele/ralph/presets/*.yml       (project)
  ~/.lele/ralph/presets/*.yml     (user global)
"""

import os
import re
import shutil
import time
import threading
from pathlib import Path

import json
import yaml

from lele_harness.engine.config import log

# ── Constants ──────────────────────────────────────────────────────────────────

XML_EVENT_RE = re.compile(r"<event\s+topic\s*=\s*\"([^\"]+)\"[^>]*>.*?</event\s*>", re.DOTALL)
LEGACY_EVENT_RE = re.compile(r">>>\s*EVENT:\s*(\S+)")

# ── State ──────────────────────────────────────────────────────────────────────

_loop_state: dict | None = None
_widget = None
_api = None  # set by register()
_lock = threading.Lock()
_do_steer = lambda _: None  # set by register()
_plan_active = False


# ── Preset loading ─────────────────────────────────────────────────────────────

def _plugin_dir() -> Path:
    return Path(__file__).resolve().parent


def _load_presets_from_dir(d: Path) -> dict:
    out = {}
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.yml")):
        try:
            cfg = yaml.safe_load(f.read_text())
            if isinstance(cfg, dict) and "event_loop" in cfg and "hats" in cfg:
                out[f.stem] = _parse_preset(cfg)
        except Exception as exc:
            log.warning("ralph: skipping preset %s: %s", f.name, exc)
    return out


def _parse_preset(raw: dict) -> dict:
    """Parse and normalize a raw YAML preset into a typed config."""
    hats = {}
    for key, h in raw.get("hats", {}).items():
        hats[key] = {
            "name": h.get("name", key),
            "description": h.get("description", ""),
            "triggers": list(h.get("triggers", [])),
            "publishes": list(h.get("publishes", [])),
            "default_publishes": h.get("default_publishes"),
            "instructions": h.get("instructions", ""),
            "disallowed_tools": list(h.get("disallowed_tools", [])) if h.get("disallowed_tools") else None,
            "max_activations": h.get("max_activations"),
            "single_task": h.get("single_task", False),
        }
    return {
        "event_loop": {
            "starting_event": raw.get("event_loop", {}).get("starting_event"),
            "completion_promise": raw.get("event_loop", {}).get("completion_promise", "LOOP_COMPLETE"),
            "max_iterations": raw.get("event_loop", {}).get("max_iterations", 50),
            "max_runtime_seconds": raw.get("event_loop", {}).get("max_runtime_seconds"),
        },
        "hats": hats,
        "core": raw.get("core"),
    }


def _load_all_presets() -> dict:
    builtin = _load_presets_from_dir(_plugin_dir() / "presets")
    user = _load_presets_from_dir(Path.home() / ".lele" / "ralph" / "presets")
    project = _load_presets_from_dir(Path(".lele") / "ralph" / "presets")
    return {**builtin, **user, **project}


# ── Event Detection (matching pi-ralph lib.ts) ─────────────────────────────────

def _detect_published_event(text: str, hat: dict) -> str | None:
    """Detect published event from assistant text.

    Matches pi-ralph's detectPublishedEvent: checks XML <event topic="..."> first,
    then legacy >>> EVENT: format, then falls back to None."""
    # 1. XML-style event tag (preferred)
    xml_matches = list(XML_EVENT_RE.finditer(text))
    if xml_matches:
        last = xml_matches[-1]
        topic = last.group(1)
        if topic in hat.get("publishes", []):
            return topic
        return hat.get("default_publishes")

    # 2. Legacy >>> EVENT: format
    m = LEGACY_EVENT_RE.search(text)
    if m:
        event_name = m.group(1).replace("<<<", "").strip()
        if event_name in hat.get("publishes", []):
            return event_name
        return hat.get("default_publishes")

    return None


def _contains_completion_promise(texts: list[str], promise: str) -> bool:
    """Check if the completion promise is found in any text.

    Matches pi-ralph's containsCompletionPromise: strips XML event tags to avoid
    false positives, checks the last non-empty line of each text block."""
    for text in reversed(texts):
        stripped = XML_EVENT_RE.sub("", text)
        for line in reversed(stripped.split("\n")):
            trimmed = line.strip()
            if not trimmed:
                continue
            if trimmed == promise:
                return True
            if trimmed == f">>> {promise}":
                return True
            bare = re.sub(r"^[*_`#\s]+|[*_`#\s]+$", "", trimmed)
            if bare == promise:
                return True
            if bare == f">>> {promise}":
                return True
            break
    return False


def _find_hat_for_event(event: str, preset: dict) -> str | None:
    """Return the first hat key whose triggers include `event`."""
    for key, hat in preset["hats"].items():
        if event in hat.get("triggers", []):
            return key
    return None


def _validate_preset(name: str, preset: dict) -> list[dict]:
    """Validate preset structure. Matches pi-ralph's validatePreset."""
    issues = []
    hat_keys = list(preset["hats"].keys())
    for key, hat in preset["hats"].items():
        if not hat.get("instructions"):
            issues.append({"level": "error", "message": f'[{name}] hat "{key}" has no instructions'})
        if len(hat.get("triggers", [])) == 0:
            issues.append({"level": "error", "message": f'[{name}] hat "{key}" has no triggers'})
        if len(hat.get("publishes", [])) == 0:
            issues.append({"level": "error", "message": f'[{name}] hat "{key}" has no publishable events'})
    start_event = preset["event_loop"].get("starting_event")
    if start_event:
        if not _find_hat_for_event(start_event, preset):
            issues.append({"level": "error", "message": f'[{name}] starting_event "{start_event}" no matching hat'})
    for hat in preset["hats"].values():
        for event in hat.get("publishes", []):
            consumer = _find_hat_for_event(event, preset)
            is_promise = event == preset["event_loop"]["completion_promise"]
            if not consumer and not is_promise:
                issues.append({"level": "warning", "message": f'[{name}] event "{event}" from "{hat["name"]}" has no consumer'})
    promise = preset["event_loop"]["completion_promise"]
    has_term = any(promise in hat.get("instructions", "") for hat in preset["hats"].values())
    if not has_term:
        issues.append({"level": "warning", "message": f'[{name}] no hat mentions "{promise}"'})
    if len(hat_keys) >= 3:
        order = {k: i for i, k in enumerate(hat_keys)}
        loopback = False
        for hk, hat in preset["hats"].items():
            for event in hat.get("publishes", []):
                consumer = _find_hat_for_event(event, preset)
                if consumer and order.get(consumer, 0) <= order.get(hk, 0):
                    loopback = True
        if not loopback:
            issues.append({"level": "warning", "message": f"[{name}] no loop-back event"})
    return issues


def _infer_event_from_content(text: str, hat: dict) -> str | None:
    """Safety-net heuristic for multi-event hats."""
    if len(hat.get("publishes", [])) <= 1:
        return None
    lower = text.lower()
    reject_pats = ["needs fix", "changes requested", "not approved", "fix required",
        "issues found", "must be fixed", "failed", "cannot approve"]
    approve_pats = ["lgtm", "looks good", "approved", "all checks pass", "ship it",
        "ready to commit", "no issues found", "passes"]
    has_rej = any(p in lower for p in reject_pats)
    has_app = any(p in lower for p in approve_pats)
    neg_kw = ["reject", "fail", "change", "request", "block", "error"]
    pos_kw = ["approve", "pass", "success", "ready", "complete"]
    def by_kw(kws):
        for e in hat["publishes"]:
            if any(k in e.lower() for k in kws):
                return e
        return None
    if has_rej and not has_app:
        return by_kw(neg_kw) or hat.get("default_publishes")
    if has_app and not has_rej:
        return by_kw(pos_kw) or hat.get("default_publishes")
    return None

def _detect_stale_cycle(history: list) -> bool:
    """Detect when the loop is stuck in a repeating hat cycle.

    Matches pi-ralph's detectStaleCycle: tries all possible cycle lengths
    (2 to len/3) and checks whether the last THREE full cycles match."""
    if len(history) < 6:
        return False
    keys = [f"{h['hat_key']}:{h['event']}" for h in history]
    min_repeats = 3
    max_cycle_len = len(keys) // min_repeats

    for cycle_len in range(2, max_cycle_len + 1):
        cycle = keys[-cycle_len:]
        repeats = 1
        for r in range(2, min_repeats + 1):
            start = len(keys) - r * cycle_len
            if start < 0:
                break
            prev = keys[start:start + cycle_len]
            if prev == cycle:
                repeats += 1
            else:
                break
        if repeats >= min_repeats:
            return True
    return False


# ── Loop Records (persistence) ──────────────────────────────────────────────

def _save_loop_record(state: dict) -> None:
    """Save loop record to .ralph/loops/. Matches pi-ralph's saveLoopRecord."""
    if not state.get("start_time"):
        return
    loops_dir = Path(state.get("cwd", os.getcwd())) / ".ralph" / "loops"
    loops_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime(state["start_time"]))
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", state.get("preset_name", "loop"))
    path = loops_dir / f"{ts}-{safe}.json"
    record = {
        "preset_name": state.get("preset_name"),
        "prompt": state.get("prompt"),
        "start_time": state.get("start_time"),
        "end_time": state.get("end_time", time.time()),
        "outcome": state.get("end_reason", "unknown"),
        "iterations": state.get("iteration", 0),
        "history": [
            {"hat": h["hat_key"], "event": h["event"], "iteration": h["iteration"]}
            for h in state.get("history", [])
        ],
        "iteration_logs": state.get("iteration_logs", []),
    }
    path.write_text(json.dumps(record, indent=2))


def _load_loop_records(cwd: str | None = None) -> list[dict]:
    """Load past loop records from .ralph/loops/."""
    loops_dir = Path(cwd or os.getcwd()) / ".ralph" / "loops"
    if not loops_dir.is_dir():
        return []
    records = []
    for f in sorted(loops_dir.glob("*.json"), reverse=True):
        try:
            rec = json.loads(f.read_text())
            if rec.get("start_time") and rec.get("preset_name"):
                records.append(rec)
        except Exception:
            continue
    return records


def _persist_loop_state() -> None:
    """Persist active loop state to .ralph/state.json."""
    state = _loop_state
    if state is None or not state.get("active"):
        return
    state_dir = Path(state.get("cwd", os.getcwd())) / ".ralph"
    state_dir.mkdir(parents=True, exist_ok=True)
    serializable = {k: v for k, v in state.items() if k != "preset"}
    serializable["preset_name"] = state.get("preset_name")
    serializable["event_loop"] = state["preset"]["event_loop"]
    (state_dir / "state.json").write_text(json.dumps(serializable, indent=2))


def _restore_loop_state() -> dict | None:
    """Restore persisted loop state."""
    state_path = Path(os.getcwd()) / ".ralph" / "state.json"
    if not state_path.is_file():
        return None
    try:
        data = json.loads(state_path.read_text())
        presets = _load_all_presets()
        pn = data.get("preset_name", "")
        if pn not in presets:
            return None
        data["preset"] = presets[pn]
        data["loop_triggered_turn"] = data.get("loop_triggered_turn", True)
        return data
    except Exception:
        return None



# ── PDD Prompt ───────────────────────────────────────────────────────────────

_PDD_PROMPT = """## Prompt-Driven Development (PDD)

Transform a rough idea into a detailed design with an implementation plan.

### Important Rules
- **User-driven flow:** Never proceed without explicit confirmation.
- **Record as you go:** Write findings to files in real time.
- **Planning only:** Produce artifacts. Do NOT implement code.

### Steps

**1. Create Project Structure**
Derive task_name as kebab-case. Create:
- `specs/{task_name}/rough-idea.md`
- `specs/{task_name}/requirements.md`
- `specs/{task_name}/research/`

Gate: Wait for user confirmation.

**2. Requirements Clarification**
Ask ONE question at a time. Append each Q&A to requirements.md.
Ask when requirements are complete.

Gate: Do not proceed without confirmation.

**3. Research**
Propose a research plan, then investigate. Document findings in research/.
Check in with user periodically.

Gate: Do not proceed without confirmation.

**4. Iteration Checkpoint**
Summarize state. Ask: Proceed to design? More requirements? Research?

**5. Create Detailed Design**
Write `specs/{task_name}/design.md` with:
- Overview, Requirements, Architecture (Mermaid diagrams)
- Components, Data Models, Error Handling
- Acceptance Criteria (Given-When-Then)

Gate: Wait for approval.

**6. Implementation Plan**
Write `specs/{task_name}/plan.md` — numbered steps.
Each step: objective, guidance, test requirements.

Gate: Wait for approval.

**7. Summary**
List all artifacts and next steps.

**8. Offer Ralph Integration**
Ask: use `/ralph code-assist` or `/ralph spec-driven` for implementation.
"""


# ── Hat Injection ──────────────────────────────────────────────────────

def _extract_next_task(scratchpad_content: str) -> dict | None:
    """Parse a scratchpad task checklist and return info about the next unchecked task."""
    tasks = []
    for line in scratchpad_content.split("\n"):
        m = re.match(r"^[-*]\s+\[([ xX])\]\s*(?:\d+\.\s*)?(.+)", line.strip())
        if m:
            tasks.append({"checked": m.group(1).lower() == "x", "description": m.group(2).strip()})
    if not tasks:
        return None
    completed = sum(1 for t in tasks if t["checked"])
    next_idx = next((i for i, t in enumerate(tasks) if not t["checked"]), -1)
    if next_idx == -1:
        return None
    return {
        "task_number": next_idx + 1,
        "description": tasks[next_idx]["description"],
        "total_tasks": len(tasks),
        "completed_tasks": completed,
    }


def _build_hat_injection(hat: dict, state: dict) -> str:
    """Build the system-prompt injection for the current hat.

    Matches pi-ralph's buildHatInjection: includes hat name, iteration,
    scratchpad task (if single_task), instructions, guardrails, disallowed tools,
    steering, event protocol, and completion promise."""
    preset = state["preset"]
    event_list = "\n".join(f"  - {e}" for e in hat.get("publishes", []))
    scratchpad_path = os.path.join(state.get("cwd", os.getcwd()), ".ralph", "scratchpad.md")

    lines = [f"\n## 🎩 Ralph Orchestration — Hat: {hat.get('name', state['current_hat_key'])}\n"]
    lines.append(f"Iteration {state['iteration']}/{preset['event_loop']['max_iterations']}\n")

    # single_task: inject only the current task from scratchpad
    if hat.get("single_task"):
        try:
            sp_content = Path(scratchpad_path).read_text()
            task_info = _extract_next_task(sp_content)
            if task_info:
                lines.append(
                    f"### YOUR CURRENT TASK ({task_info['task_number']} of "
                    f"{task_info['total_tasks']}, {task_info['completed_tasks']} completed)\n\n"
                )
                lines.append(f"> **Task {task_info['task_number']}:** {task_info['description']}\n\n")
                lines.append("**This is the ONLY task you may work on.** ")
                lines.append("Do not implement anything else.\n")
                lines.append("The orchestration loop will call you again for the next task ")
                lines.append("after this one is reviewed and committed.\n\n")
        except Exception:
            pass

    lines.append(hat.get("instructions", ""))

    # Guardrails
    guardrails = preset.get("core", {}).get("guardrails", [])
    if guardrails:
        lines.append("\n\n### Guardrails\n")
        for g in guardrails:
            lines.append(f"- {g}\n")

    # Disallowed tools
    disallowed = hat.get("disallowed_tools")
    if disallowed:
        lines.append("\n\n### TOOL RESTRICTIONS\n")
        lines.append("You MUST NOT use these tools in this hat:\n")
        for tool in disallowed:
            lines.append(f"- **{tool}** — blocked for this hat\n")
        lines.append("\nUsing a restricted tool is a scope violation.\n")

    # Scratchpad context
    lines.append(f"\n\n### Scratchpad\n")
    lines.append("Each hat runs in a fresh session with no conversation history ")
    lines.append("from previous hats.\n")
    lines.append("Use the scratchpad file to pass context between hats:\n\n")
    lines.append(f"**File:** `{scratchpad_path}`\n\n")
    lines.append("- **Read it first** — the previous hat's notes are there\n")
    lines.append("- **Write your notes** before publishing your event ")
    lines.append("— the next hat will read them\n")
    lines.append("- Include: what you did, what files you changed, any issues found, ")
    lines.append("what the next hat needs to know\n")

    # User steering
    steering = state.get("steering", [])
    if steering:
        lines.append(f"\n\n### Steering from the User\n")
        lines.append("The user has provided the following guidance for this hat. ")
        lines.append("Follow these instructions:\n\n")
        for msg in steering:
            lines.append(f"- {msg}\n")

    # Event protocol
    lines.append(f"\n\n### Event Protocol\n")
    lines.append("When you have completed ALL work for this hat, publish exactly ONE ")
    lines.append("event using this XML format:\n\n")
    lines.append("```\n<event topic=\"event_name\">Brief description of what was done</event>\n```\n\n")
    lines.append("You MUST use one of these EXACT event names ")
    lines.append(f"(no other names are valid):\n{event_list}\n\n")
    lines.append("**CRITICAL:** The event tag signals the END of your work for this hat. ")
    lines.append("Do ALL your work FIRST (implementation, tests, verification), ")
    lines.append("THEN publish the event as your FINAL output. ")
    lines.append("Do NOT continue working after publishing an event.\n\n")

    # Completion promise
    completion = preset["event_loop"]["completion_promise"]
    lines.append("When the ENTIRE task is fully complete (all work done, committed, ")
    lines.append("and verified), instead output on its own line:\n")
    lines.append(f"{completion}\n\n")
    lines.append(f"Do NOT output {completion} unless ALL work is truly finished.\n")

    return "".join(lines)


# ── Widget ─────────────────────────────────────────────────────────────────────

def _update_widget() -> None:
    if _widget is None:
        return
    state = _loop_state
    if state is None or not state.get("active"):
        _widget.clear()
        return
    preset = state["preset"]
    hat = preset["hats"].get(state["current_hat_key"], {})
    hat_name = hat.get("name", state["current_hat_key"])
    iter_str = f"{state['iteration']}/{preset['event_loop']['max_iterations']}"
    pause_indicator = " ⏸ PAUSED" if state.get("paused") else ""
    lines = [
        f"[bold accent]Ralph Loop: {state['preset_name']}[/]",
        f"[accent]🎩 {hat_name}[/] [dim][{iter_str}][/]{pause_indicator}",
    ]
    for h in state["history"][-6:]:
        icon = "▸" if h["hat_key"] == state["current_hat_key"] else " "
        name = preset["hats"].get(h["hat_key"], {}).get("name", h["hat_key"])
        lines.append(f"{icon} {name} [dim]← {h['event']}[/]")
    _widget.set("\n".join(lines))


# ── Loop lifecycle ─────────────────────────────────────────────────────────────

def _start_loop(preset_name: str, prompt: str, preset: dict) -> str:
    """Start a new Ralph loop — matches pi-ralph's startLoop()."""
    global _loop_state
    with _lock:
        if _loop_state and _loop_state.get("active"):
            return "A Ralph loop is already running. Stop it first with /ralph stop."

        # Find starting hat
        start_event = preset["event_loop"].get("starting_event")
        start_hat_key = _find_hat_for_event(start_event, preset) if start_event else None
        if not start_hat_key:
            start_hat_key = next(iter(preset["hats"].keys()), None)
        if not start_hat_key:
            return "Preset has no hats defined."

        cwd = os.getcwd()

        # Create .ralph/ directory and initialize scratchpad
        ralph_dir = os.path.join(cwd, ".ralph")
        os.makedirs(ralph_dir, exist_ok=True)
        sp_path = os.path.join(ralph_dir, "scratchpad.md")
        if not os.path.exists(sp_path):
            Path(sp_path).write_text(
                f"# Ralph Scratchpad\n\nPreset: {preset_name}\nTask: {prompt}\n\n---\n\n"
            )

        _loop_state = {
            "preset_name": preset_name,
            "preset": preset,
            "current_hat_key": start_hat_key,
            "iteration": 1,
            "start_time": time.time(),
            "pending_kickoff": True,
            "prompt": prompt,
            "active": True,
            "paused": False,
            "cwd": cwd,
            "history": [{"hat_key": start_hat_key, "hat_name": preset["hats"][start_hat_key].get("name", start_hat_key), "event": start_event or "start", "iteration": 1}],
            "activations": {start_hat_key: 1},
            "steering": [],
            "iteration_logs": [],
            "loop_triggered_turn": True,
        }

    _update_widget()
    _persist_loop_state()

    # Steer the initial hat message (matches pi-ralph's sendHatMessage)
    hat = preset["hats"][start_hat_key]
    hat_name = hat.get("name", start_hat_key)
    steer_msg = (
        f"[Ralph Loop: {preset_name}] Starting with hat: {hat_name}\n\n"
        f"Task: {prompt}"
    )
    try:
        _do_steer(steer_msg)
    except Exception as exc:
        log.warning("ralph: steer failed: %s", exc)

    return f"Ralph loop started: {preset_name} — {prompt}"


def _stop_loop(reason: str) -> None:
    """Stop the loop and save record — matches pi-ralph's stopLoop()."""
    global _loop_state
    state = _loop_state
    if state is None:
        return

    # Save loop record
    state["active"] = False
    state["end_time"] = time.time()
    _save_loop_record(state)
    _update_widget()

    # Clean up state file
    state_path = Path(state.get("cwd", os.getcwd())) / ".ralph" / "state.json"
    if state_path.exists():
        state_path.unlink()

    _loop_state = None


# ── Orchestration Decision (matching pi-ralph's determineNextAction) ────────────

def _determine_next_action(ctx: dict) -> dict:
    """Pure decision function: given loop state after an agent turn, determine action.

    Returns one of:
      {"type": "skip", "reason": ...}
      {"type": "complete"}
      {"type": "stop", "reason": ...}
      {"type": "continue", "next_hat_key": ..., "event": ...}

    Matches pi-ralph's determineNextAction ordering."""
    # Skip: not a loop-triggered turn (user message during loop)
    if not ctx.get("loop_triggered_turn"):
        return {"type": "skip", "reason": "user-turn"}

    # Skip: loop is paused
    if ctx.get("paused"):
        return {"type": "skip", "reason": "paused"}

    # Skip: pending kickoff (first turn after start)
    if ctx.get("pending_kickoff"):
        return {"type": "skip", "reason": "pending-kickoff"}

    # Complete: completion promise found
    if ctx.get("completion_found"):
        return {"type": "complete"}

    # Stop: max iterations
    preset = ctx["preset"]
    if ctx["iteration"] >= preset["event_loop"]["max_iterations"]:
        return {"type": "stop", "reason": f"Max iterations reached ({preset['event_loop']['max_iterations']})"}

    # Stop: max runtime
    max_runtime = preset["event_loop"].get("max_runtime_seconds")
    if max_runtime:
        elapsed = time.time() - ctx["start_time"]
        if elapsed >= max_runtime:
            return {"type": "stop", "reason": f"Max runtime reached ({max_runtime}s)"}

    # Stop: no event published (stalled)
    if not ctx.get("published_event"):
        return {"type": "stop", "reason": "No event published — loop stalled"}

    # Find next hat for the event
    next_hat_key = _find_hat_for_event(ctx["published_event"], preset)
    if not next_hat_key:
        # Terminal event — loop complete
        return {"type": "complete"}

    # Stop: max_activations exhausted for the next hat
    next_hat_config = preset["hats"].get(next_hat_key)
    if next_hat_config and next_hat_config.get("max_activations"):
        count = ctx["activations"].get(next_hat_key, 0) + 1
        if count > next_hat_config["max_activations"]:
            return {
                "type": "stop",
                "reason": f'Hat "{next_hat_config["name"]}" exhausted ({next_hat_config["max_activations"]} activations)',
            }

    # Complete: stale cycle detected
    tentative_history = ctx.get("history", []) + [
        {"hat_key": next_hat_key, "event": ctx["published_event"], "iteration": ctx["iteration"] + 1}
    ]
    if _detect_stale_cycle(tentative_history):
        return {"type": "complete"}

    # Continue to next hat
    return {"type": "continue", "next_hat_key": next_hat_key, "event": ctx["published_event"]}


# ── Hooks ──────────────────────────────────────────────────────────────────────

def _on_turn_start(ctx: dict) -> str | None:
    """Inject hat instructions or PDD prompt."""
    if _plan_active:
        return _PDD_PROMPT

    state = _loop_state
    if state is None or not state.get("active"):
        return None
    hat = state["preset"]["hats"].get(state["current_hat_key"])
    if hat is None:
        return None

    injection = _build_hat_injection(hat, state)

    # Clear steering after injection
    state["steering"] = []

    return injection


def _on_turn_end(ctx: dict) -> None:
    """Detect events and advance loop — matches pi-ralph's agent_end handler."""
    state = _loop_state
    if state is None or not state.get("active"):
        return

    text = ctx.get("text", "")
    messages = ctx.get("messages", [])
    preset = state["preset"]
    current_hat = preset["hats"].get(state["current_hat_key"])

    # Collect all assistant message texts
    assistant_texts = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant":
            content = m.get("content", "")
            if isinstance(content, str):
                assistant_texts.append(content)
            elif isinstance(content, list):
                assistant_texts.append("".join(
                    c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
                ))

    # Detect completion promise from ALL assistant messages
    promise = preset["event_loop"]["completion_promise"]
    completion_found = _contains_completion_promise(assistant_texts, promise)

    # Detect event from ALL assistant messages
    published_event = None
    if current_hat:
        for t in reversed(assistant_texts):
            published_event = _detect_published_event(t, current_hat)
            if published_event:
                break
        if published_event is None and current_hat.get("default_publishes"):
            published_event = current_hat["default_publishes"]
        if published_event is None:
            published_event = _infer_event_from_content(text, current_hat)

    # Capture iteration log
    def _capture_iteration_log(event_name: str):
        state["iteration_logs"].append({
            "iteration": state["iteration"],
            "hat_key": state["current_hat_key"],
            "hat_name": current_hat.get("name", state["current_hat_key"]) if current_hat else state["current_hat_key"],
            "event": event_name,
            "summary": text[:2000],
            "timestamp": time.time(),
        })

    # Handle user-turn auto-resume
    if not state.get("loop_triggered_turn") and state.get("paused"):
        state["paused"] = False
        _update_widget()

    # Determine next action
    action = _determine_next_action({
        "completion_found": completion_found,
        "published_event": published_event,
        "loop_triggered_turn": state.get("loop_triggered_turn", False),
        "paused": state.get("paused", False),
        "iteration": state["iteration"],
        "start_time": state["start_time"],
        "preset": preset,
        "current_hat_key": state["current_hat_key"],
        "history": state["history"],
        "activations": state["activations"],
    })

    # Apply action
    if action["type"] == "skip":
        if action.get("reason") == "user-turn":
            state["loop_triggered_turn"] = True  # Re-arm
        elif action.get("reason") == "pending-kickoff":
            state["pending_kickoff"] = False
        return

    if action["type"] == "complete":
        if completion_found:
            _capture_iteration_log(promise)
        elif published_event:
            _capture_iteration_log(published_event)
        _stop_loop("Task complete ✓")
        return

    if action["type"] == "stop":
        _stop_loop(action["reason"])
        return

    if action["type"] == "continue":
        next_hat_key = action["next_hat_key"]
        event_name = action["event"]
        _capture_iteration_log(event_name)

        # Advance loop
        state["current_hat_key"] = next_hat_key
        state["iteration"] += 1
        state["loop_triggered_turn"] = True
        state["activations"][next_hat_key] = state["activations"].get(next_hat_key, 0) + 1
        state["history"].append({
            "hat_key": next_hat_key,
            "hat_name": preset["hats"][next_hat_key].get("name", next_hat_key),
            "event": event_name,
            "iteration": state["iteration"],
        })

        _update_widget()
        _persist_loop_state()

        # Steer next hat message
        next_hat = preset["hats"][next_hat_key]
        steer_msg = (
            f"[Ralph Loop — Iteration {state['iteration']}/{preset['event_loop']['max_iterations']}]\n"
            f"Event: {event_name} → Hat: {next_hat['name']}\n\n"
            f"Task: {state['prompt']}\n\n"
            f"Read the scratchpad at `{state['cwd']}/.ralph/scratchpad.md` "
            f"for context from the previous hat."
        )
        try:
            _do_steer(steer_msg)
        except Exception as exc:
            log.warning("ralph: steer failed: %s", exc)


# ── History / Loops overlays ────────────────────────────────────────────────
# Matches pi-ralph's ctx.ui.custom(...) browsers (index.ts ~488-575, ~577-720+):
# arrow-key navigation over the same render(width)/handle_input(key)/done(result)
# contract lele's api.overlay() already exposes (see doom's plugin for the other
# real user of it in this codebase).

def _popup_size() -> tuple[int, int]:
    cols, rows = shutil.get_terminal_size()
    return min(100, int(cols * 0.8)), min(32, int((rows - 4) * 0.8))


def _format_duration(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    m, rem = divmod(s, 60)
    return f"{m}m{rem}s"


_LOG_MAX_VISIBLE = 16


def _render_iteration_log(log: dict, idx: int, total: int, scroll: int) -> tuple[list[str], int]:
    """Renders one iteration log entry, clamping `scroll` to its valid range.
    Returns (lines, clamped_scroll) — the caller stores the clamped value back."""
    summary_lines = str(log.get("summary", "")).split("\n")
    max_scroll = max(0, len(summary_lines) - _LOG_MAX_VISIBLE)
    scroll = min(scroll, max_scroll)

    lines = [
        f"[bold accent]Iteration {log['iteration']}[/] [dim]({idx + 1}/{total})[/]",
        "",
        f"[dim]Hat:[/] {log.get('hat_name', log.get('hat_key', '?'))}",
        f"[dim]Event:[/] {log.get('event', '?')}",
        f"[dim]Time:[/] {time.strftime('%H:%M:%S', time.localtime(log.get('timestamp', 0)))}",
        "",
    ]
    visible = summary_lines[scroll:scroll + _LOG_MAX_VISIBLE]
    lines.extend(visible)
    if len(summary_lines) > _LOG_MAX_VISIBLE:
        lines.append("")
        end = min(scroll + _LOG_MAX_VISIBLE, len(summary_lines))
        lines.append(f"[dim][{scroll + 1}-{end}/{len(summary_lines)} lines][/]")
    return lines, scroll


class _HistoryOverlay:
    """/ralph history — browse the CURRENT loop's iteration logs."""

    def __init__(self, logs: list[dict]):
        self.done = lambda _: None
        self.popup_size = _popup_size()
        self._logs = logs
        self._idx = len(logs) - 1
        self._scroll = 0

    def render(self, width: int) -> list[str]:
        lines, self._scroll = _render_iteration_log(self._logs[self._idx], self._idx, len(self._logs), self._scroll)
        lines.append("")
        lines.append("[dim]←/→ iteration • ↑/↓ scroll • esc close[/]")
        return lines

    def handle_input(self, key: str) -> None:
        if key == "left" and self._idx > 0:
            self._idx -= 1
            self._scroll = 0
        elif key == "right" and self._idx < len(self._logs) - 1:
            self._idx += 1
            self._scroll = 0
        elif key == "up":
            self._scroll = max(0, self._scroll - 1)
        elif key == "down":
            self._scroll += 1


class _LoopsOverlay:
    """/ralph loops — browse past loop records: a list (↑/↓ select, enter to
    view), and per-record iteration-log detail (←/→ between logs, esc back)."""

    esc_closes = False

    def __init__(self, records: list[dict]):
        self.done = lambda _: None
        self.popup_size = _popup_size()
        self._records = records
        self._selected = 0
        self._list_scroll = 0
        self._detail = False
        self._log_idx = 0
        self._log_scroll = 0

    def _render_list(self) -> list[str]:
        lines = [f"[bold accent]Past Ralph Loops[/] [dim]({len(self._records)})[/]", ""]
        max_visible = 12
        max_scroll = max(0, len(self._records) - max_visible)
        self._list_scroll = min(self._list_scroll, max_scroll)
        visible = self._records[self._list_scroll:self._list_scroll + max_visible]
        for i, r in enumerate(visible):
            idx = self._list_scroll + i
            cursor = "▸" if idx == self._selected else " "
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("start_time", 0)))
            dur = _format_duration(r.get("end_time", 0) - r.get("start_time", 0))
            outcome = str(r.get("outcome", "unknown"))
            lines.append(f"{cursor} {r.get('preset_name', '?')} "
                         f"[dim]— {when} — {dur} — {r.get('iterations', 0)} iters[/]")
            lines.append(f"  {outcome} [dim]— {str(r.get('prompt', ''))[:80]}[/]")
        lines.append("")
        lines.append("[dim]↑/↓ select • enter view • esc close[/]")
        return lines

    def _render_detail(self) -> list[str]:
        record = self._records[self._selected]
        logs = record.get("iteration_logs", [])
        lines = [
            f"[bold accent]{record.get('preset_name', '?')}[/] "
            f"[dim]— {time.strftime('%Y-%m-%d %H:%M', time.localtime(record.get('start_time', 0)))}[/]",
            f"[dim]Prompt:[/] {str(record.get('prompt', ''))[:80]}",
            f"[dim]Outcome:[/] {record.get('outcome', 'unknown')}",
            f"[dim]Duration:[/] {_format_duration(record.get('end_time', 0) - record.get('start_time', 0))}"
            f" [dim]— {record.get('iterations', 0)} iterations[/]",
            "",
        ]
        if not logs:
            lines.append("[dim]No iteration logs recorded.[/]")
        else:
            log_lines, self._log_scroll = _render_iteration_log(
                logs[self._log_idx], self._log_idx, len(logs), self._log_scroll)
            lines.extend(log_lines)
        lines.append("")
        lines.append("[dim]←/→ iteration • ↑/↓ scroll • esc back[/]")
        return lines

    def render(self, width: int) -> list[str]:
        return self._render_detail() if self._detail else self._render_list()

    def handle_input(self, key: str) -> None:
        if key == "escape":
            if self._detail:
                self._detail = False
                self._log_idx = 0
                self._log_scroll = 0
            else:
                self.done(None)
            return
        if not self._detail:
            if key == "up" and self._selected > 0:
                self._selected -= 1
            elif key == "down" and self._selected < len(self._records) - 1:
                self._selected += 1
            elif key == "enter":
                self._detail = True
                self._log_idx = 0
                self._log_scroll = 0
            return
        logs = self._records[self._selected].get("iteration_logs", [])
        if key == "left" and self._log_idx > 0:
            self._log_idx -= 1
            self._log_scroll = 0
        elif key == "right" and self._log_idx < len(logs) - 1:
            self._log_idx += 1
            self._log_scroll = 0
        elif key == "up":
            self._log_scroll = max(0, self._log_scroll - 1)
        elif key == "down":
            self._log_scroll += 1


# ── Command ────────────────────────────────────────────────────────────────────

def _ralph_command(app, arg: str) -> None:
    parts = (arg or "").strip().split(None, 1)
    cmd = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    if cmd == "stop":
        if _loop_state and _loop_state.get("active"):
            _stop_loop("Stopped by user")
            app._append("[yellow]Ralph loop stopped.[/]")
        else:
            app._append("[dim]No active loop.[/]")
        return

    if cmd == "status":
        state = _loop_state
        if state and state.get("active"):
            hat = state["preset"]["hats"].get(state["current_hat_key"], {})
            hat_name = hat.get("name", state["current_hat_key"])
            elapsed = int(time.time() - state["start_time"])
            paused = " ⏸ PAUSED" if state.get("paused") else ""
            app._append(
                f"[accent]Ralph:[/] {state['preset_name']} "
                f"[dim]🎩 {hat_name} ({state['iteration']}/{state['preset']['event_loop']['max_iterations']})"
                f" [{elapsed}s]{paused}[/]"
            )
        else:
            app._append("[dim]No active Ralph loop.[/]")
        return

    if cmd == "steer":
        msg = rest.strip()
        if not msg:
            app._append("[yellow]Usage: /ralph steer <message>[/]")
            return
        if not (_loop_state and _loop_state.get("active")):
            app._append("[yellow]No active loop to steer.[/]")
            return
        # Feeds state["steering"], read + cleared by _build_hat_injection/_on_turn_start
        # (that half already existed — nothing ever appended to it until now). Not
        # api.steer(): that injects into the live conversation, which a hat-transition
        # context reset would just discard — this needs to survive in plugin state.
        _loop_state.setdefault("steering", []).append(msg)
        app._append(f"[accent]Steering queued[/] ({len(_loop_state['steering'])} pending). "
                    f"Will be injected into the next hat.")
        return

    if cmd == "pause":
        if not (_loop_state and _loop_state.get("active")):
            app._append("[yellow]No active loop to pause.[/]")
            return
        if _loop_state.get("paused"):
            app._append("[dim]Loop is already paused.[/]")
            return
        _loop_state["paused"] = True
        _update_widget()
        _persist_loop_state()
        app._append("[accent]⏸ Loop paused.[/] The loop will not auto-continue after this turn. "
                    "Use /ralph resume to continue, or send any message.")
        return

    if cmd == "resume":
        if not (_loop_state and _loop_state.get("active")):
            app._append("[yellow]No active loop to resume.[/]")
            return
        if not _loop_state.get("paused"):
            app._append("[dim]Loop is not paused.[/]")
            return
        _loop_state["paused"] = False
        _update_widget()
        _persist_loop_state()
        app._append("[accent]▶ Loop resumed.[/] Will continue after this turn completes.")
        return

    if cmd == "history":
        if _loop_state is None:
            app._append("[dim]No loop state available.[/]")
            return
        logs = _loop_state.get("iteration_logs", [])
        if not logs:
            app._append("[dim]No iteration history yet.[/]")
            return
        if _api is not None:
            _api.overlay(_HistoryOverlay(logs))
        return

    if cmd == "loops":
        cwd = _loop_state.get("cwd", os.getcwd()) if _loop_state else os.getcwd()
        records = _load_loop_records(cwd)
        if not records:
            app._append("[dim]No past loops found.[/]")
            return
        if _api is not None:
            _api.overlay(_LoopsOverlay(records))
        return

    if cmd == "presets":
        presets = _load_all_presets()
        if not presets:
            app._append("[dim]No presets found.[/]")
            return
        lines = ["[accent]Available presets:[/]"]
        for name, cfg in presets.items():
            hats = " → ".join(cfg.get("hats", {}).keys())
            lines.append(f"  [bold]{name}[/] — {hats}")
        app._append("\n".join(lines))
        return

    # Start loop: <preset> <prompt>
    if cmd == "start":
        preset_parts = rest.split(None, 1)
        preset_name = preset_parts[0] if preset_parts else ""
        prompt = preset_parts[1] if len(preset_parts) > 1 else ""
    else:
        preset_name = cmd
        prompt = rest

    if not preset_name:
        app._append("[yellow]Usage: /ralph <preset> <prompt> | /ralph stop|status|presets[/]")
        return

    presets = _load_all_presets()
    if preset_name not in presets:
        available = ", ".join(presets.keys())
        app._append(f"[red]Unknown preset '{preset_name}'. Available: {available}[/]")
        return

    if _loop_state and _loop_state.get("active"):
        app._append("[yellow]A loop is already running. Use /ralph stop first.[/]")
        return

    if not prompt:
        prompt_parts = rest.split(None, 1)
        if len(prompt_parts) > 1:
            prompt = prompt_parts[1]
        if not prompt:
            app._append("[yellow]Prompt required. Usage: /ralph {preset_name} <your task>[/]")
            return

    _start_loop(preset_name, prompt, presets[preset_name])
    app._append(f"[accent]Ralph loop started:[/] {preset_name} — {prompt}")



# ── PDD /plan command ──────────────────────────────────────────────────────

def _plan_command(app, arg: str) -> None:
    """Handle /plan <idea>."""
    global _plan_active
    if _loop_state and _loop_state.get("active"):
        app._append("[yellow]A Ralph loop is active. Use /ralph stop first.[/]")
        return
    idea = (arg or "").strip()
    if not idea:
        app._append("[yellow]Usage: /plan <rough idea>[/]")
        return
    _plan_active = True
    # Inject PDD prompt via steer — the turn_start hook will inject it next turn
    msg = f"[PDD Planning] Transform this idea:\n\n{idea}\n\n{_PDD_PROMPT}"
    try:
        _do_steer(msg)
    except Exception:
        pass
    app._append(f"[accent]PDD session started for:[/] {idea}")


def _on_plan_turn_start(ctx: dict) -> str | None:
    """Inject PDD prompt when plan mode is active."""
    if not _plan_active:
        return None
    return _PDD_PROMPT


# ── Tool ───────────────────────────────────────────────────────────────────────

def _start_loop_tool(args, cwd=None, cancel=None) -> str:
    preset_name = (args.get("preset") or "").strip()
    prompt = (args.get("prompt") or "").strip()
    presets = _load_all_presets()
    if preset_name not in presets:
        available = ", ".join(presets.keys())
        return f'Unknown preset "{preset_name}". Available: {available}'
    if not prompt:
        return "Prompt is required."
    return _start_loop(preset_name, prompt, presets[preset_name])


# ── Register ───────────────────────────────────────────────────────────────────

def register(api) -> None:
    global _widget, _do_steer, _api

    _api = api
    _do_steer = api.steer if hasattr(api, "steer") else lambda _: None
    _widget = api.widget("ralph-loop")

    api.command("/ralph", "Start/stop/show Ralph orchestration loops", _ralph_command, sub={
        "start <preset> <prompt>": "start a loop with a preset and task prompt",
        "stop": "stop the current loop",
        "status": "show current loop status",
        "pause": "pause the loop",
        "resume": "resume the loop",
        "steer <msg>": "send guidance to current hat",
        "history": "show iteration logs",
        "loops": "browse past loop records",
        "presets": "list available presets",
    })

    api.tool(
        name="start_ralph_loop",
        description=(
            "Start a Ralph orchestration loop for multi-step tasks. "
            "Use when a task benefits from hat-based orchestration "
            "(planning, building, reviewing). Presets: feature, code-assist, "
            "spec-driven, refactor, review, debug."
        ),
        parameters={
            "type": "object",
            "properties": {
                "preset": {
                    "type": "string",
                    "description": "Preset name: feature, code-assist, spec-driven, refactor, review, debug",
                },
                "prompt": {
                    "type": "string",
                    "description": "Task description for the loop",
                },
            },
            "required": ["preset", "prompt"],
        },
        execute=_start_loop_tool,
    )

    if hasattr(api, "on"):
        api.on("turn_start", _on_turn_start)
        api.on("turn_end", _on_turn_end)
        api.command("/plan", "Start a PDD planning session", _plan_command, sub={
            "<idea>": "a rough idea to refine into a plan",
        })

    _update_widget()
