"""Internationalization (presentation layer only).

i18n NEVER touches the deterministic payload (code, symbol_id, graph, scores, commit_sha). It only
localizes human-readable text: error messages, disambiguation prompts, coverage explanations, and
log labels. This boundary is what preserves P1 (same task+commit+permission -> byte-identical
payload) while still letting tool *messages* be Turkish or English.
"""

from bce.core.i18n.locale import normalize_locale, resolve_locale
from bce.core.i18n.translator import Translator, get_translator

__all__ = ["normalize_locale", "resolve_locale", "Translator", "get_translator"]
