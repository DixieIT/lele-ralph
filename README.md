# lele-ralph

Hat-based multi-agent orchestration loops for [lele-harness](https://github.com/DixieIT/lele-harness).
Keeps the agent iterating through specialized "hats" (roles) until a task is complete.
Ported from [pi-ralph](https://github.com/samfoy/pi-ralph).

## Features

- **Hat-based orchestration** — define specialized roles (Planner, Builder, Reviewer, etc.)
  that hand off work via events
- **Built-in presets** — ready-to-use workflows for common tasks
- **Custom presets** — create your own YAML-based workflows
- **Event protocol** — hats publish events that trigger the next hat, forming an
  autonomous loop
- **Guard rails** — max iterations, max runtime, and completion promises prevent
  runaway loops
- **Widget** — persistent status panel showing current hat, iteration, and loop history

## Install

From inside lele:

```
/plugins install DixieIT/lele-ralph
```

or manually:

```bash
git clone https://github.com/DixieIT/lele-ralph ~/.lele/plugins/ralph
```

Then restart lele or run `/reload`.

## Use

### Commands

| Command | What it does |
|---|---|
| `/ralph <preset> <prompt>` | Start a loop |
| `/ralph stop` | Stop the current loop |
| `/ralph status` | Show current loop state |
| `/ralph presets` | List available presets |

### Tool (LLM-callable)

`start_ralph_loop({preset, prompt})` — the model can start loops autonomously.

### Examples

```
/ralph feature Add user authentication with JWT
/ralph code-assist Implement rate limiting for the API
/ralph debug Tests fail intermittently in CI
/ralph refactor Extract auth logic into a separate module
/ralph review Review changes in src/auth/
```

## Presets

| Preset | Hats | Use case |
|---|---|---|
| **feature** | Planner → Builder → Reviewer → Committer | General feature development |
| **code-assist** | Planner → Builder → Validator → Committer | Full TDD pipeline with quality gate |
| **spec-driven** | Specifier → Implementer → Verifier → Committer | Spec-first development |
| **debug** | Investigator → Tester → Fixer → Verifier | Scientific debugging |
| **refactor** | Analyzer → Preparer → Refactorer → Verifier | Safe refactoring with tests |
| **review** | Explorer → Reviewer → Reporter | Code review |

## How It Works

1. The loop starts with the first hat defined by the preset's `starting_event`
2. Each turn, the hat's instructions are injected into the system prompt as a
   `<harness>` note
3. The agent works under that hat's guidance
4. When done, the agent publishes an event: `>>> EVENT: event_name`
5. Ralph detects the event and finds the next hat that triggers on it
6. The next hat's instructions are injected, and the loop continues
7. When the task is complete, the agent outputs `<<< LOOP_COMPLETE >>>`

## Custom Presets

Create YAML preset files in any of these locations (project overrides user overrides
built-in):

1. `<plugin>/presets/*.yml` (built-in)
2. `~/.lele/ralph/presets/*.yml` (user)
3. `.lele/ralph/presets/*.yml` (project)

### Format

```yaml
event_loop:
  starting_event: "build.start"       # Event that triggers the first hat
  completion_promise: "LOOP_COMPLETE"  # Magic string output when done
  max_iterations: 50                   # Safety limit
  max_runtime_seconds: 14400           # Optional timeout

hats:
  planner:
    name: "📋 Planner"
    description: "Creates implementation plan"
    triggers: ["build.start"]
    publishes: ["tasks.ready"]
    default_publishes: "tasks.ready"
    instructions: |
      ## PLANNER MODE
      Your detailed instructions here...
```

## License

MIT
