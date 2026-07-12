"""Ralph Loop — hat-based multi-agent orchestration for lele.

Port of pi-ralph (samfoy/pi-ralph). Keeps the agent iterating through
specialized hats until a task is complete. Each hat has its own instructions,
triggers, and events that drive the workflow forward.

Commands:
  /ralph <preset> <prompt>  — start a loop
  /ralph stop               — stop the current loop
  /ralph status             — show loop status
  /ralph presets            — list available presets

Tools:
  start_ralph_loop({preset, prompt}) — LLM-callable loop start

Presets loaded from (project overrides user overrides built-in):
  <plugin>/presets/*.yml          (built-in)
  .lele/ralph/presets/*.yml       (project)
  ~/.lele/ralph/presets/*.yml     (user global)
"""

import os
import re
import time
import threading
from pathlib import Path

import yaml

from lele_harness.engine.config import log

# ── Constants ──────────────────────────────────────────────────────────────────

XML_EVENT_RE = re.compile(r"<event\s+topic\s*=\s*\"([^\"]+)\"[^>]*>.*?</event\s*>", re.DOTALL)
LEGACY_EVENT_RE = re.compile(r">>>\s*EVENT:\s*(\S+)")

# ── State ──────────────────────────────────────────────────────────────────────

_loop_state: dict | None = None
_widget = None
_lock = threading.Lock()
_do_steer = lambda _: None  # set by register()


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


# ── Hat Injection (matching pi-ralph's buildHatInjection) ──────────────────────

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
    _update_widget()

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
    """Inject hat instructions — matches pi-ralph's before_agent_start handler."""
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
    global _widget, _do_steer

    _do_steer = api.steer if hasattr(api, "steer") else lambda _: None
    _widget = api.widget("ralph-loop")

    api.command("/ralph", "Start/stop/show Ralph orchestration loops", _ralph_command, sub={
        "start <preset> <prompt>": "start a loop with a preset and task prompt",
        "stop": "stop the current loop",
        "status": "show current loop status",
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

    _update_widget()
