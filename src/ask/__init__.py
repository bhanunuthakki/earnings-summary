"""The unified conversational engine ("one brain, two entry points").

Both chat surfaces — the command center's Ask tab (``POST /api/ask``) and
the per-report chat drawer (``POST /chat/<ticker>``) — route every turn
through ``ask.engine.respond_turn``. What differs between them is only the
attached :class:`ask.context.ContextPack`: a report drawer turn carries
that ticker's report context (thesis, bear case, prior threads — the
existing ``chat_session`` machinery), while an Ask-tab turn carries the
portfolio-scoped pack.

Import submodules directly (``from ask.engine import respond_turn``); this
package init stays import-free so ``chat_session`` ⇄ ``ask`` can reference
each other's submodules without an import cycle.
"""
