# AGENTS.md

## Python environment

Use the project virtualenv for all Python commands:

```sh
.venv/bin/python -m pytest
.venv/bin/python -m compileall vesper tests
```

Do not use system `python`, `pytest`, or package commands unless explicitly asked.

## Test commands

Preferred full verification:

```sh
.venv/bin/python -m pytest -q
```

For focused checks:

```sh
.venv/bin/python -m pytest tests/test_service.py -q
```

## Notes

The system Python environment may not have all project dependencies installed.
