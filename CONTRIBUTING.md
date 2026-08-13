# Contributing

## Setup

Install `uv`, Python 3.10 or later, and tmux, then create the development
environment:

```bash
uv sync --frozen
```

Keep changes focused and add tests for public behavior. Tool descriptions
should describe the tool generally rather than a particular change or
environment.

## Checks

Run the standard checks before opening a pull request:

```bash
uv lock --check
uv run --frozen ruff format --check .
uv run --frozen ruff check .
uv run --frozen pytest -q
```

Changes to terminal transport, pane recovery, or file transfer should also run
the real tmux suite:

```bash
RSM_RUN_TMUX_TESTS=1 uv run --frozen pytest -q tests/test_write_tmux.py
```

CI additionally checks the supported Python and MCP SDK matrices, the built
wheel, and plugin metadata.
