"""UI adapter layer (optional, self-contained).

This package exposes read-only ``/v1/ui/*`` endpoints that serve graph data in a
visualisation-friendly shape (``{nodes: [...], edges: [...]}``) for any frontend.

It is deliberately isolated from the rest of the engine:

- All Cypher/SQL reads live in :mod:`cce.api.rest.ui.queries` (nothing is added to
  ``GraphRepository`` or the tool layers).
- The only integration point is the ``ui_router`` registration in ``cce.api.rest.app``.

To remove the UI layer entirely: delete this package and the clearly-marked
"UI layer" block in ``cce/api/rest/app.py``. No other code depends on it.
"""

from cce.api.rest.ui.router import router as ui_router

__all__ = ["ui_router"]
