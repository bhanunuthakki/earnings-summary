from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from pipeline import work_os_shell
from pipeline.cc_action import CC_ACTION_CSS
from pipeline.cc_overlay import CC_OVERLAY_CSS
from pipeline.work_os_copilot import render_work_os_copilot
from pipeline.work_os_styles import (
    CC_ACTION_CSS as MASTER_ACTION_CSS,
)
from pipeline.work_os_styles import (
    CC_OVERLAY_CSS as MASTER_OVERLAY_CSS,
)
from pipeline.work_os_styles import (
    WORK_OS_COPILOT_CSS,
    WORK_OS_CSS,
)


def test_work_os_runtime_uses_visual_master_compositions() -> None:
    production_runtime = cast(
        Callable[[datetime], str], getattr(work_os_shell, "_production_runtime")
    )
    runtime = production_runtime(datetime.now(UTC))
    assert WORK_OS_CSS in runtime
    assert MASTER_ACTION_CSS == CC_ACTION_CSS
    assert MASTER_OVERLAY_CSS == CC_OVERLAY_CSS


def test_copilot_emits_master_css() -> None:
    rendered = render_work_os_copilot()
    assert WORK_OS_COPILOT_CSS in rendered


def test_consumers_have_no_local_visual_selector_blocks() -> None:
    root = Path(__file__).parents[1] / "src" / "pipeline"
    consumers = [
        *(path for path in root.glob("work_os_*.py") if path.name != "work_os_styles.py"),
        root / "cc_action.py",
        root / "cc_overlay.py",
        root / "ticker_command_center.py",
    ]
    selector = re.compile(r"(?m)^\s*(?:\.[A-Za-z][\w-]*|#[A-Za-z][\w-]*)\s*\{")
    offenders = {
        str(path): selector.findall(path.read_text(encoding="utf-8")) for path in consumers
    }
    assert not {path: hits for path, hits in offenders.items() if hits}
