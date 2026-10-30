"""Say-Do report reads recorded management commitments and outcomes."""

from __future__ import annotations

from collections.abc import Callable
from io import StringIO
from typing import cast

import report.renderers.markdown as markdown_renderer
from report.models import SayDoSection

render_saydo_markdown = cast(
    "Callable[[StringIO, SayDoSection], None]", getattr(markdown_renderer, "_saydo")
)
