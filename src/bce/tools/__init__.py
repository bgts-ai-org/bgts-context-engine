"""Tool surface (Layer 1/2/3).

Phase 0 implements the Layer-1 deterministic primitives. Layer 2 (hybrid retrieval) and Layer 3
(task-aware orchestration) arrive in later phases. Every tool keeps the deterministic payload
language-neutral and localizes only the human-readable ``message`` field (i18n boundary).
"""
