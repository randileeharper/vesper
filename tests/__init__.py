# Marks ``tests`` as a package so tooling (mypy) resolves ``conftest`` under a
# single module name (``tests.conftest``) rather than both ``conftest`` and
# ``tests.conftest``. This also matches the existing
# ``from tests.conftest import ...`` imports in the suite. See issue #82.
