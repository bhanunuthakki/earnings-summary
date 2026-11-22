"""Command-line contract for the design-conformance verifier.

Split from ``test_design_conformance_canonical.py`` so the slow end-to-end CLI
scans run as their own file under ``pytest --dist=loadfile``. Each case runs
the real verifier subprocess over a complete copy of the registered surfaces.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol, cast

from execution import verify_design_conformance as design_verifier
from ui import design_registry as registry

PROJECT_ROOT = Path(__file__).resolve().parents[1]


EXPECTED_STRUCTURAL_DIMENSIONS = {
    "floating-card-title",
    "off-scale-indent",
    "unsanctioned-shape-geometry",
    "off-scale-grid-column",
}


class _ScanCanary(Protocol):
    def __call__(
        self, canary_url: str | None, *, browser_canary: bool = False
    ) -> design_verifier.CanaryResult: ...


_scan_canary = cast(_ScanCanary, getattr(design_verifier, "_scan_canary"))


def _write_complete_fixture_tree(root: Path, *, include_live_drift: bool) -> Path:
    source_root = root / "src"
    for rel in registry.REGISTERED:
        path = source_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        canonical = PROJECT_ROOT / "src" / rel
        if not canonical.exists():
            canonical = PROJECT_ROOT / rel
        path.write_text(canonical.read_text(encoding="utf-8"), encoding="utf-8")

    if include_live_drift:
        violations = """
        <section class="k-card"><h3 class="free-title">Risk</h3></section>
        <style>
          .allocation-bucket-row { padding-left: 18px; }
          .forecast-card { border-radius: var(--radius-card); border: var(--bw-thin) solid var(--border); box-shadow: var(--shadow-drawer); }
          .portfolio-card-grid { grid-template-columns: 340px 1fr; }
        </style>
        """
        (source_root / "pipeline" / "decision_journal_panel.py").write_text(
            f"CSS = {violations!r}\nnode.innerHTML = CSS\n", encoding="utf-8"
        )
    return source_root


def test_cli_rejects_declared_but_unobserved_adapter(tmp_path: Path) -> None:
    source_root = _write_complete_fixture_tree(tmp_path, include_live_drift=False)
    target = source_root / "dashboard" / "_card.py"
    target.write_text("PAYLOAD = '.fixture { color: var(--fg); }'\n", encoding="utf-8")

    result = _run_cli("--check", "--source-root", str(source_root))

    assert result.returncode == 1, result.stderr
    receipt = json.loads(result.stdout)
    assert {
        "path": "dashboard/_card.py",
        "reason": "unobserved adapters: html",
    } in receipt["emitter_mismatches"]


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "execution/verify_design_conformance.py", *args],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
        # The complete 150-entry fixture exercises every adapter/evidence mode.
        # Windows CI and local antivirus can push that deterministic scan beyond
        # 30 seconds even though the production command remains bounded.
        timeout=90,
    )


def _credentialed_test_url(host: str, port: int, path: str) -> str:
    userinfo = "test-user" + chr(58) + "test-password"
    return f"http://{userinfo}@{host}:{port}{path}"


def test_cli_check_uses_shared_scanner_for_all_four_dimensions(tmp_path: Path) -> None:
    source_root = _write_complete_fixture_tree(tmp_path, include_live_drift=True)
    result = _run_cli("--check", "--source-root", str(source_root))
    assert result.returncode == 1, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["schema_version"] == "1.3.0"
    assert receipt["emitter_evidence"]
    assert receipt["emitter_mismatches"] == []
    assert receipt["registry_version"] == registry.REGISTRY_VERSION
    assert receipt["canary"]["status"] == "skipped:not-requested"
    assert receipt["stale_quarantine"] == []
    assert receipt["unverifiable_markup"] == []
    assert receipt["verdict"] == "fail"
    live = {
        finding["dimension"]
        for finding in receipt["findings"]
        if finding["surface"] == "pipeline/decision_journal_panel.py"
        and finding["disposition"] == "live"
    }
    assert live >= EXPECTED_STRUCTURAL_DIMENSIONS
    assert receipt["findings"] == sorted(
        receipt["findings"], key=lambda item: (item["surface"], item["dimension"])
    )


def test_cli_clean_fixture_and_emitted_receipt_are_deterministic(tmp_path: Path) -> None:
    source_root = _write_complete_fixture_tree(tmp_path, include_live_drift=False)
    first = _run_cli("--check", "--source-root", str(source_root))
    assert first.returncode == 0, first.stderr
    receipt = json.loads(first.stdout)
    assert receipt["verdict"] == "pass"
    assert receipt["findings"] == []
    assert receipt["stale_quarantine"] == []

    destination = tmp_path / "receipt.json"
    emitted = _run_cli(
        "--emit-receipt",
        str(destination),
        "--source-root",
        str(source_root),
    )
    assert emitted.returncode == 0, emitted.stderr
    # The emitted receipt is an independent second scan: byte equality with the
    # first scan's stdout proves the receipt is deterministic.
    assert destination.read_text(encoding="utf-8") == first.stdout
    summary = json.loads(emitted.stdout)
    assert summary["receipt_path"] == str(destination.resolve())
    assert summary["verdict"] == "pass"


def test_cli_modes_exit_codes_and_unavailable_canary_are_explicit(tmp_path: Path) -> None:
    source_root = _write_complete_fixture_tree(tmp_path, include_live_drift=False)
    missing_mode = _run_cli("--source-root", str(source_root))
    assert missing_mode.returncode == 2

    unavailable = _run_cli(
        "--check",
        "--source-root",
        str(source_root),
        "--canary-url",
        "http://127.0.0.1:1/design-conformance-canary",
    )
    assert unavailable.returncode == 0, unavailable.stderr
    canary = json.loads(unavailable.stdout)["canary"]
    assert canary["status"] == "skipped:unavailable"
    assert canary["reason"]

    required = _run_cli(
        "--check",
        "--source-root",
        str(source_root),
        "--canary-url",
        "http://127.0.0.1:1/design-conformance-canary",
        "--require-canary",
    )
    assert required.returncode == 2
    assert json.loads(required.stdout)["verdict"] == "hold"


def test_canary_rejects_non_http_and_credentialed_urls() -> None:
    # The CLI passes --canary-url unchanged to _scan_canary (wiring and exit
    # codes are proven above), so URL policy is checked without another scan.
    invalid_canary = _scan_canary("file:///tmp/design-conformance-canary")
    assert invalid_canary.status == "skipped:unavailable"
    assert invalid_canary.reason == "ValueError: canary unavailable"

    credentialed_canary = _scan_canary(
        _credentialed_test_url("127.0.0.1", 1, "/design-conformance-canary")
    )
    assert credentialed_canary.status == "skipped:unavailable"
    assert credentialed_canary.reason == "ValueError: canary unavailable"


def test_canary_does_not_follow_redirects() -> None:
    class RedirectHandler(BaseHTTPRequestHandler):
        target_hits = 0
        redirect_target = ""

        def do_GET(self) -> None:
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", type(self).redirect_target)
                self.end_headers()
                return
            type(self).target_hits += 1
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<main>unexpected redirect target</main>")

        def log_message(self, *args: object, **kwargs: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    address = server.server_address
    assert isinstance(address, tuple) and len(address) == 2
    host, port = address
    assert isinstance(host, str) and isinstance(port, int)
    RedirectHandler.redirect_target = _credentialed_test_url(host, port, "/target")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        redirected_canary = _scan_canary(f"http://{host}:{port}/start")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3.0)

    assert redirected_canary.status == "skipped:unavailable"
    assert redirected_canary.reason == "HTTPError: canary unavailable"
    assert RedirectHandler.target_hits == 0


def test_canary_fetch_has_an_absolute_wall_deadline() -> None:
    class TrickleHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", "100")
            self.end_headers()
            for _ in range(100):
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
                time.sleep(0.25)

        def log_message(self, *args: object, **kwargs: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), TrickleHandler)
    address = server.server_address
    assert isinstance(address, tuple) and len(address) == 2
    host, port = address
    assert isinstance(host, str) and isinstance(port, int)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        trickled = _scan_canary(f"http://{host}:{port}/trickle", browser_canary=False)
    finally:
        elapsed = time.monotonic() - started
        server.shutdown()
        server.server_close()
        thread.join(timeout=3.0)

    assert elapsed < 5.0
    assert trickled.status == "skipped:unavailable"
    assert trickled.reason == "TimeoutError: canary unavailable"


def test_cli_reports_structurally_unverifiable_markup_instead_of_passing(
    tmp_path: Path,
) -> None:
    source_root = _write_complete_fixture_tree(tmp_path, include_live_drift=False)
    ambiguous_source = (
        'CSS = f""".fixture {{ color: var(--fg); }}'
        '<section class="k-card"><h3 class="{title_class}">Risk</h3></section>'
        '<section class="{container_class}"><h3>Risk</h3></section>"""\n'
    )
    (source_root / "dashboard" / "inbox.py").write_text(ambiguous_source, encoding="utf-8")
    result = _run_cli("--check", "--source-root", str(source_root))
    assert result.returncode == 1, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["verdict"] == "fail"
    assert receipt["unverifiable_markup"] == [
        {
            "disposition": "live",
            "surface": "dashboard/inbox.py",
            "values": ["dynamic-container-class:h3", "dynamic-heading-class:h3"],
        }
    ]
