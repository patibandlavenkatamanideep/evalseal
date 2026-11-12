"""Reading another tool's results into a receipt.

One adapter, `promptfoo`. The shared half is in `base.py` and is deliberately small: a
normalized observation, the list of fields an export could not fill, and the function
that assembles a record from them. A registry and a plugin protocol would be written
against a single example, which is how an abstraction that fits nothing gets built. The
second adapter decides what is shared.
"""

from .base import EXPORT_ROLE, Gaps, ImportRefused, Observation, build_record

__all__ = ["EXPORT_ROLE", "Gaps", "ImportRefused", "Observation", "build_record"]
