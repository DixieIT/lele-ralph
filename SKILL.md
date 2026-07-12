# Ralph Loop — Hat-based orchestration for lele

Port of [pi-ralph](https://github.com/samfoy/pi-ralph). Keeps the agent
iterating through specialized hats (roles) until a task is complete.

## Usage

### Slash commands

```
/ralph start <preset> <task description>   — start a loop
/ralph stop                                 — stop current loop
/ralph status                               — show loop status
/ralph presets                              — list available presets
```

### Tools (LLM-callable)

- `start_ralph_loop({preset, prompt})` — start an orchestration loop

## Presets

| Preset | Hats | Use case |
|---|---|---|
| `feature` | Planner → Builder → Reviewer → Committer | Standard feature dev |
| `code-assist` | Planner → Builder → Validator → Committer | TDD pipeline with quality gate |
| `debug` | Investigator → Tester → Fixer → Verifier | Scientific debugging |
| `review` | Explorer → Reviewer → Reporter | Code review |
| `refactor` | Analyzer → Preparer → Refactorer → Verifier | Safe refactoring |
| `spec-driven` | Specifier → Implementer → Verifier → Committer | Spec-first development |

## Protocol

Each hat receives instructions in a harness note at the start of every turn.
When done, the agent publishes an event:

```
>>> EVENT: event_name
```

The loop finds the next hat triggered by that event and repeats.
When the task is fully complete, output the completion promise:

```
<<< LOOP_COMPLETE >>>
```

Custom presets go in `~/.lele/ralph/presets/*.yml` (user) or
`.lele/ralph/presets/*.yml` (project).
