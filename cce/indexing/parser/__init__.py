"""Multi-language parser layer (abstraction-first).

Every language implements :class:`cce.indexing.parser.base.LanguageProvider`. The
:class:`cce.indexing.parser.registry.LanguageRegistry` maps file extensions to providers, so adding
a language never touches the extractor or the rest of the engine.
"""
