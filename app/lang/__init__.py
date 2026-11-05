"""The Keel language: how a builder (the AI, a person on the App map, an outside agent) makes and changes an app.

It sits above the design the engine runs (app/blueprint): typed (app/lang/model.py), explicit (every name resolved,
every path spelt out), with features for common needs (app/lang/patterns.py) that compile to the right rules every
time and write their own checks. Everything about it the AI is told comes from one registry (app/lang/registry.py).

    apply(design, patches)   -> Outcome: the new design, what changed, a problem for each patch refused
    describe(design)         -> the app in the builder's terms
    checks(design)           -> every feature's own checks, for the simulator to act out
    catalog()                -> the language, as the AI is told it
"""
from .compile import Outcome, apply, kinds, patch_type  # noqa: F401
from .patterns import checks  # noqa: F401
from .read import describe  # noqa: F401
from .registry import catalog, fingerprint  # noqa: F401
