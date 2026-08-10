"""The unified durable conversational engine behind Work OS Copilot.

Production turns enter through ``POST /api/ask`` or ``/api/ask/stream`` and
route through ``ask.engine.respond_turn`` with a typed context pack. Reports
hand ticker/report identity into that same workspace; legacy ``/chat`` routes
are non-writing migration tombstones.

Import submodules directly (``from ask.engine import respond_turn``); this
package init remains import-free to avoid widening the engine's import graph.
"""
