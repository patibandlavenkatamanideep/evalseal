"""`python -m evalseal`, for when the console script is not on PATH.

A pip install into a virtual environment that has not been activated, a `--user`
install on a machine whose user scripts directory is not in PATH, or a Windows shell
that has not picked up a new entry: in all of them the package is importable and
`evalseal` is not runnable. This is the documented way through, so it has to exist -
docs/operations.md promised `python -m evalseal.cli` before either spelling worked.
"""

from .cli import app

app()
