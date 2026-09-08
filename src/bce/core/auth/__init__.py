"""Auth / Scope filter (spec section 9).

A deterministic, identity-bound filter that removes symbols in repos the caller may not read, plus an
audit_log writer for traceability ("why did the AI change this code?"). Enforced in Phase 4; earlier
phases pass an allow-all principal.
"""

from bce.core.auth.scope import Principal, ScopeFilter, write_audit_log

__all__ = ["Principal", "ScopeFilter", "write_audit_log"]
