"""Tests for /ralph command dispatch — pause/resume/steer were documented in
the help text and completion menu but never wired into _ralph_command, so
typing them fell through to being treated as an unknown preset name
(reported live: `/ralph resume` -> "Unknown preset 'resume'")."""
from unittest.mock import MagicMock

import plugin


def _fresh_state(**overrides):
    state = {
        "active": True, "paused": False, "steering": [],
        "preset_name": "feature", "current_hat_key": "plan",
        "preset": {"hats": {"plan": {"name": "Plan"}}, "event_loop": {"max_iterations": 10}},
        "iteration": 1, "start_time": 0,
    }
    state.update(overrides)
    return state


def test_resume_reports_no_active_loop_instead_of_unknown_preset():
    plugin._loop_state = None
    app = MagicMock()
    plugin._ralph_command(app, "resume")
    msg = app._append.call_args[0][0]
    assert "No active loop" in msg
    assert "Unknown preset" not in msg


def test_resume_unpauses_active_loop(monkeypatch):
    plugin._loop_state = _fresh_state(paused=True)
    monkeypatch.setattr(plugin, "_update_widget", lambda: None)
    monkeypatch.setattr(plugin, "_persist_loop_state", lambda: None)
    app = MagicMock()
    plugin._ralph_command(app, "resume")
    assert plugin._loop_state["paused"] is False
    assert "resumed" in app._append.call_args[0][0].lower()


def test_resume_noop_when_not_paused():
    plugin._loop_state = _fresh_state(paused=False)
    app = MagicMock()
    plugin._ralph_command(app, "resume")
    assert "not paused" in app._append.call_args[0][0].lower()


def test_pause_sets_flag(monkeypatch):
    plugin._loop_state = _fresh_state(paused=False)
    monkeypatch.setattr(plugin, "_update_widget", lambda: None)
    monkeypatch.setattr(plugin, "_persist_loop_state", lambda: None)
    app = MagicMock()
    plugin._ralph_command(app, "pause")
    assert plugin._loop_state["paused"] is True


def test_pause_reports_no_active_loop():
    plugin._loop_state = None
    app = MagicMock()
    plugin._ralph_command(app, "pause")
    assert "No active loop" in app._append.call_args[0][0]


def test_pause_noop_when_already_paused():
    plugin._loop_state = _fresh_state(paused=True)
    app = MagicMock()
    plugin._ralph_command(app, "pause")
    assert "already paused" in app._append.call_args[0][0].lower()


def test_steer_appends_to_loop_state_steering_list():
    """Must feed state["steering"] (read + cleared by _build_hat_injection /
    _on_turn_start) — not api.steer(), which injects into live conversation
    history that a hat-transition context reset would just discard."""
    plugin._loop_state = _fresh_state()
    app = MagicMock()
    plugin._ralph_command(app, "steer focus on edge cases")
    assert plugin._loop_state["steering"] == ["focus on edge cases"]


def test_steer_reports_no_active_loop():
    plugin._loop_state = None
    app = MagicMock()
    plugin._ralph_command(app, "steer do something")
    assert "No active loop" in app._append.call_args[0][0]


def test_steer_requires_a_message():
    plugin._loop_state = _fresh_state()
    app = MagicMock()
    plugin._ralph_command(app, "steer")
    assert "Usage" in app._append.call_args[0][0]


def test_unknown_preset_still_reports_correctly():
    """Regression guard: a genuinely unknown preset name must still hit the
    'Unknown preset' path, not get swallowed by the new subcommands."""
    plugin._loop_state = None
    app = MagicMock()
    plugin._ralph_command(app, "totally-not-a-preset some task")
    assert "Unknown preset" in app._append.call_args[0][0]


# ── /ralph history + /ralph loops: also documented, never wired ─────────────

def _log(iteration, hat_name="Plan", event="planned", summary="did the thing"):
    return {"iteration": iteration, "hat_key": hat_name.lower(), "hat_name": hat_name,
            "event": event, "summary": summary, "timestamp": 0}


def test_history_reports_no_loop_state():
    plugin._loop_state = None
    app = MagicMock()
    plugin._ralph_command(app, "history")
    assert "No loop state" in app._append.call_args[0][0]


def test_history_reports_no_iterations_yet():
    plugin._loop_state = _fresh_state(iteration_logs=[])
    app = MagicMock()
    plugin._ralph_command(app, "history")
    assert "No iteration history" in app._append.call_args[0][0]


def test_history_opens_overlay_with_logs(monkeypatch):
    logs = [_log(1), _log(2)]
    plugin._loop_state = _fresh_state(iteration_logs=logs)
    fake_api = MagicMock()
    monkeypatch.setattr(plugin, "_api", fake_api)
    app = MagicMock()
    plugin._ralph_command(app, "history")
    fake_api.overlay.assert_called_once()
    component = fake_api.overlay.call_args[0][0]
    assert isinstance(component, plugin._HistoryOverlay)
    assert component._logs == logs


def test_loops_reports_no_past_loops(monkeypatch):
    plugin._loop_state = None
    monkeypatch.setattr(plugin, "_load_loop_records", lambda cwd=None: [])
    app = MagicMock()
    plugin._ralph_command(app, "loops")
    assert "No past loops" in app._append.call_args[0][0]


def test_loops_opens_overlay_with_records(monkeypatch):
    records = [{"preset_name": "feature", "start_time": 0, "end_time": 10,
                "outcome": "done", "iterations": 3, "prompt": "x", "iteration_logs": []}]
    plugin._loop_state = None
    monkeypatch.setattr(plugin, "_load_loop_records", lambda cwd=None: records)
    fake_api = MagicMock()
    monkeypatch.setattr(plugin, "_api", fake_api)
    app = MagicMock()
    plugin._ralph_command(app, "loops")
    fake_api.overlay.assert_called_once()
    component = fake_api.overlay.call_args[0][0]
    assert isinstance(component, plugin._LoopsOverlay)
    assert component._records == records


# ── _HistoryOverlay ──────────────────────────────────────────────────────────

def test_history_overlay_starts_on_last_iteration():
    ov = plugin._HistoryOverlay([_log(1), _log(2), _log(3)])
    assert ov._idx == 2
    assert "Iteration 3" in "\n".join(ov.render(80))


def test_history_overlay_navigates_left_right():
    ov = plugin._HistoryOverlay([_log(1), _log(2), _log(3)])
    ov.handle_input("left")
    assert ov._idx == 1
    ov.handle_input("left")
    assert ov._idx == 0
    ov.handle_input("left")  # clamped at 0
    assert ov._idx == 0
    ov.handle_input("right")
    assert ov._idx == 1


def test_history_overlay_scroll_clamped_and_resets_on_navigation():
    long_summary = "\n".join(f"line {i}" for i in range(50))
    ov = plugin._HistoryOverlay([_log(1, summary=long_summary), _log(2)])
    ov.handle_input("left")  # move to iteration 1 (long summary)
    for _ in range(100):
        ov.handle_input("down")  # scroll way past the end
    ov.render(80)
    assert ov._scroll <= 50  # clamped, not runaway
    ov.handle_input("right")  # navigating resets scroll
    assert ov._scroll == 0


# ── _LoopsOverlay ────────────────────────────────────────────────────────────

def _record(name, logs=None):
    return {"preset_name": name, "start_time": 0, "end_time": 5, "outcome": "done",
            "iterations": 1, "prompt": "task", "iteration_logs": logs or []}


def test_loops_overlay_list_navigation():
    ov = plugin._LoopsOverlay([_record("a"), _record("b"), _record("c")])
    assert ov._selected == 0
    ov.handle_input("down")
    assert ov._selected == 1
    ov.handle_input("up")
    assert ov._selected == 0
    ov.handle_input("up")  # clamped at 0
    assert ov._selected == 0


def test_loops_overlay_enter_opens_detail_esc_goes_back_not_close():
    ov = plugin._LoopsOverlay([_record("a", logs=[_log(1)])])
    done = MagicMock()
    ov.done = done
    ov.handle_input("enter")
    assert ov._detail is True
    ov.handle_input("escape")
    assert ov._detail is False
    done.assert_not_called()  # first esc backs out of detail, doesn't close
    ov.handle_input("escape")
    done.assert_called_once_with(None)  # second esc (from list) actually closes


def test_loops_overlay_detail_navigates_between_logs():
    ov = plugin._LoopsOverlay([_record("a", logs=[_log(1), _log(2)])])
    ov.handle_input("enter")
    assert ov._log_idx == 0
    ov.handle_input("right")
    assert ov._log_idx == 1
    ov.handle_input("right")  # clamped at last log
    assert ov._log_idx == 1
    lines = "\n".join(ov.render(80))
    assert "Iteration 2" in lines


def test_loops_overlay_detail_handles_record_with_no_logs():
    ov = plugin._LoopsOverlay([_record("a", logs=[])])
    ov.handle_input("enter")
    lines = "\n".join(ov.render(80))
    assert "No iteration logs recorded" in lines
