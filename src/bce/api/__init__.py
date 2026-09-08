"""Interface layer (thin adapters).

MCP and REST call the same core/tools; logic is never duplicated here (spec section 7). These
adapters only translate transport <-> tool calls and resolve the request locale for the i18n
presentation layer.
"""
