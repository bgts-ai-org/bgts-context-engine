"""Token-Budget Assembler (spec section 6.5).

Fits the ranked candidate list into a token budget deterministically: take from the top until the
budget is exhausted, assigning each item a summary level (full body / signature / reference-only)
by its graph distance. Same input + budget -> same assembled package (P1).
"""

from cce.core.assembler.assembler import DetailLevel, assemble

__all__ = ["assemble", "DetailLevel"]
