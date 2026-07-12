"""Ralph Loop — hat-based multi-agent orchestration for lele.

Port of pi-ralph (samfoy/pi-ralph). Keeps the agent iterating through
specialized hats (Planner → Builder → Reviewer → ...) until a task is done.

Commands:
  /ralph [preset] [prompt]  — start a loop
  /ralph stop               — stop the current loop
  /ralph status             — show loop status
  /ralph presets            — list available presets

Tools:
  start_ralph_loop({preset, prompt}) — LLM-callable loop start

Presets loaded from:
  <plugin>/presets/*.yml        (built-in)
  .lele/ralph/presets/*.yml     (project)
  ~/.lele/ralph/presets/*.yml   (user global)
"""
import os
import re
import time
import threading
from pathlib import Path

import yaml

from lele_harness.engine.config import log

# ── Types ──────────────────────────────────────────────────────────────────────
_HAT_RE = re.compile(r">>>\s*EVENT:\s*(\S+)")
_COMPLETION_RE = re.compile(r"<<<\s*\w+_COMPLETE\s*>>>")


# ── State ──────────────────────────────────────────────────────────────────────

_loop_state: dict | None = None
_widget = None
_lock = threading.Lock()


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
                out[f.stem] = cfg
        except Exception as exc:
            log.warning("ralph: skipping preset %s: %s", f.name, exc)
    return out


def _load_all_presets() -> dict:
    builtin = _load_presets_from_dir(_plugin_dir() / "presets")
    user = _load_presets_from_dir(Path.home() / ".lele" / "ralph" / "presets")
    project = _load_presets_from_dir(Path.cwd() / ".lele" / "ralph" / "presets")
    return {**builtin, **user, **project}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _detect_event(text: str, hat: dict) -> str | None:
    """Look for `>>> EVENT: name` in text."""
    m = _HAT_RE.search(text)
    if m:
        return m.group(1)
    return None


def _find_hat_for_event(event: str, preset: dict) -> str | None:
    """Return the first hat key whose triggers include `event`."""
    for key, hat in preset["hats"].items():
        if event in hat.get("triggers", []):
            return key
    return None


def _build_hat_injection(hat: dict, state: dict) -> str:
    """Build the system-prompt injection for the current hat."""
    lines = [
        "---",
        f"## 🎩 {hat.get('name', state['current_hat_key'])}",
        f"**Role:** {hat.get('description', '')}",
        "",
        hat.get("instructions", ""),
        "",
    ]
    if state.get("history"):
        lines.append("**Loop history:**")
        for h in state["history"][-5:]:
            lines.append(f"  - {h['hat_name']} ← {h['event']}")
        lines.append("")

    lines.extend([
        "**Protocol:**",
        f"- When done, publish an event with `>>> EVENT: <event_name>`",
        f"- Available events to publish: {', '.join(hat.get('publishes', []))}",
        f"- When the entire task is finished, output `<<< LOOP_COMPLETE >>>`",
        "",
        "---",
    ])
    return "\n".join(lines)


def _update_widget() -> None:
    if _widget is None:
        return
    state = _loop_state
    if state is None or not state.get("active"):
        _widget.clear()
        return
    hat = state["preset"]["hats"].get(state["current_hat_key"], {})
    hat_name = hat.get("name", state["current_hat_key"])
    iter_str = f"{state['iteration']}/{state['preset']['event_loop']['max_iterations']}"
    lines = [
        f"[bold accent]Ralph Loop: {state['preset_name']}[/]",
        f"[accent]🎩 {hat_name}[/] [dim][{iter_str}][/]",
    ]
    for h in state["history"][-6:]:
        icon = "▸" if h["hat_key"] == state["current_hat_key"] else " "
        name = state["preset"]["hats"].get(h["hat_key"], {}).get("name", h["hat_key"])
        lines.append(f"{'▸' if h['hat_key'] == state['current_hat_key'] else ' '} {name} [dim]← {h['event']}[/]")
    _widget.set("\n".join(lines))


def _stop_loop(reason: str) -> None:
    global _loop_state
    state = _loop_state
    if state is None:
        return
    state["active"] = False
    state["end_reason"] = reason
    state["end_time"] = time.time()
    _update_widget()
    _loop_state = None


# ── Command ────────────────────────────────────────────────────────────────────

def _ralph_command(app, arg: str) -> None:
    parts = (arg or "").strip().split(None, 1)  # subcmd, [rest...]
    cmd = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    if cmd == "stop":
        if _loop_state and _loop_state.get("active"):
            _stop_loop("stopped by user")
            app._append("[yellow]Ralph loop stopped.[/]")
        else:
            app._append("[dim]No active loop.[/]")
        return

    if cmd == "status":
        state = _loop_state
        if state and state.get("active"):
            hat = state["preset"]["hats"].get(state["current_hat_key"], {})
            hat_name = hat.get("name", state["current_hat_key"])
            app._append(
                f"[accent]Ralph:[/] {state['preset_name']} "
                f"[dim]🎩 {hat_name} ({state['iteration']}/{state['preset']['event_loop']['max_iterations']})[/]"
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

    # Anything else: first word = preset name, rest = prompt
    # /ralph feature add login → preset=feature, prompt="add login"
    # /ralph start feature add login → cmd="start", preset="feature", rest="add login"
    if cmd == "start":
        preset_name = rest.split(None, 1)[0] if rest else ""
        prompt = rest.split(None, 1)[1] if " " in rest else ""
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
        app._append("[yellow]Prompt required. Usage: /ralph {preset_name} <your task>[/]")
        return

    _start_loop(preset_name, prompt, presets[preset_name])
    app._append(f"[accent]Ralph loop started:[/] {preset_name} — {prompt}")


# ── Tool ───────────────────────────────────────────────────────────────────────

def _start_loop(preset_name: str, prompt: str, preset: dict) -> str:
    global _loop_state
    with _lock:
        if _loop_state and _loop_state.get("active"):
            return "A Ralph loop is already running. Stop it first with /ralph stop."

        start_event = preset["event_loop"].get("starting_event")
        start_hat_key = _find_hat_for_event(start_event, preset) if start_event else None
        if not start_hat_key:
            start_hat_key = next(iter(preset["hats"].keys()), None)
        if not start_hat_key:
            return "Preset has no hats defined."

        _loop_state = {
            "preset_name": preset_name,
            "preset": preset,
            "current_hat_key": start_hat_key,
            "iteration": 1,
            "start_time": time.time(),
            "prompt": prompt,
            "active": True,
            "history": [{"hat_key": start_hat_key, "hat_name": preset["hats"][start_hat_key].get("name", start_hat_key), "event": start_event or "start", "iteration": 1}],
            "max_iterations": preset["event_loop"]["max_iterations"],
            "max_runtime": preset["event_loop"].get("max_runtime_seconds", 0),
        }

    _update_widget()

    # Steer the first hat message into the conversation so the model
    # sees the task prompt as a user message, just like pi-ralph's followUp
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


def _on_turn_start(ctx: dict) -> str | None:
    """Inject hat instructions as a harness note at turn start."""
    state = _loop_state
    if state is None or not state.get("active"):
        return None
    hat = state["preset"]["hats"].get(state["current_hat_key"])
    if hat is None:
        return None
    return _build_hat_injection(hat, state)


def _on_turn_end(ctx: dict) -> None:
    """Detect events and advance the loop."""
    state = _loop_state
    if state is None or not state.get("active"):
        return

    text = ctx.get("text", "")
    messages = ctx.get("messages", [])

    # Check all assistant messages for events
    completion_found = bool(_COMPLETION_RE.search(text))
    published_event = None
    current_hat = state["preset"]["hats"].get(state["current_hat_key"])
    if current_hat:
        published_event = _detect_event(text, current_hat)
        if published_event is None and current_hat.get("default_publishes"):
            published_event = current_hat["default_publishes"]
    # Guard: max runtime
    max_runtime = state.get("max_runtime", 0)
    if max_runtime > 0 and (time.time() - state["start_time"]) > max_runtime:
        _stop_loop("max runtime exceeded")
        return

    # Guard: max iterations
        _stop_loop("max iterations reached")
        return

    if completion_found:
        _stop_loop("complete")
        return

    if published_event is None:
        return

    # Find next hat
    next_hat_key = _find_hat_for_event(published_event, state["preset"])
    if next_hat_key is None:
        # No hat handles this event — stop
        _stop_loop(f"no hat handles event '{published_event}'")
        return

    # Advance
    state["current_hat_key"] = next_hat_key
    state["iteration"] += 1
    state["loop_triggered_turn"] = True
    state["activations"][next_hat_key] = state["activations"].get(next_hat_key, 0) + 1
    state["history"].append({
        "hat_key": next_hat_key,
        "hat_name": state["preset"]["hats"][next_hat_key].get("name", next_hat_key),
        "event": published_event,
        "iteration": state["iteration"],
    })

    _update_widget()

    # If we have steer capability, send the next hat message
    next_hat = state["preset"]["hats"][next_hat_key]
    hat_name = next_hat.get("name", next_hat_key)
    steer_text = (
        f"[Ralph Loop: {state['preset_name']}] "
        f"Event '{published_event}' → next hat: {hat_name}\n\n"
        f"Task: {state['prompt']}"
    )
    # We can't call api.steer() from here easily — return the steer text via
    # the plugin API's steer mechanism, which we set up in register()
    try:
        _do_steer(steer_text)
    except Exception as exc:
        log.warning("ralph: steer failed: %s", exc)


_do_steer = lambda _: None  # set by register()


# ── Register ───────────────────────────────────────────────────────────────────

def register(api) -> None:
    global _widget, _do_steer

    _do_steer = api.steer if hasattr(api, "steer") else lambda _: None
    _widget = api.widget("ralph-loop")

    # Command
    api.command("/ralph", "Start/stop/show Ralph orchestration loops", _ralph_command, sub={
        "start <preset> <prompt>": "start a loop with a preset and task prompt",
        "stop": "stop the current loop",
        "status": "show current loop status",
        "presets": "list available presets",
    })

    # Tool
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

    # Hooks
    if hasattr(api, "on"):
        api.on("turn_start", _on_turn_start)
        api.on("turn_end", _on_turn_end)

    # Start with no loop
    _update_widget()
