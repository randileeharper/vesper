"""System prompts loaded from text files in this package.

Each system prompt lives as a ``.txt`` file alongside this module, one
sentence per line, so they can be read and edited without touching Python
code.  At runtime :func:`load_prompt` reads the file and joins the lines
with single spaces, reproducing the continuous string that the inline
prompt concatenations previously produced.
"""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files

__all__ = ["load_prompt"]


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """Load a system prompt from ``<name>.txt`` in this package.

    Lines are joined with single spaces to match the output of the old
    inline string concatenations.  Empty lines are skipped so the files
    can use blank separators for readability.
    """
    text = files(__package__).joinpath(f"{name}.txt").read_text(encoding="utf-8")
    return " ".join(line.strip() for line in text.splitlines() if line.strip())
