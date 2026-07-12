"""execution/run_weekly_packet.py — the Sunday packet (PR2, Deliverable 1).

Assembles the standing open-loop substrates (unreconciled notes/themes/
falsifiers, proposed Tenets, pending research proposals, stub decisions) into
ONE finite Telegram packet: a header, one card per item (LLM-pre-drafted
verdict when available, buttons always), and — once every item gets a
verdict, which may happen hours or days later via button taps handled by the
running poller — a "Packet clear" receipt.

    python execution/run_weekly_packet.py
    python execution/run_weekly_packet.py --repo-root . --db-path /tmp/x.db

The predraft LLM leg (Haiku-tier, ``weekly_packet_predraft``) degrades
per-item: a transient failure ships the item WITHOUT a suggested verdict
(buttons still work) rather than blocking the packet. Exit 0 on a completed
send pass (including zero items / capture unconfigured — not set up, not a
failure). Exit 2 only on a missing DB.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from capture import token_store  # noqa: E402
from pipeline import weekly_packet  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--db-path", type=Path, default=None)
    args = parser.parse_args(argv)

    repo_root: Path = args.repo_root.resolve()
    db_path: Path = (
        args.db_path if args.db_path is not None else repo_root / "data" / "portfolio.db"
    )
    if not db_path.exists():
        sys.stderr.write(f"FATAL: no DB at {db_path}\n")
        return 2

    import db

    db.set_db_path(db_path)  # so the predraft LLM cost rows land in THIS DB's ledger

    try:
        token = token_store.load_token(repo_root / "data" / "secrets" / "telegram_bot_token")
    except token_store.CaptureSetupError as exc:
        print(f"run_weekly_packet: not configured ({exc}); exiting cleanly", file=sys.stderr)
        return 0

    chat_id = token_store.load_chat_id(repo_root / "data" / "capture" / "telegram_chat_id.json")
    if chat_id is None:
        print(
            "run_weekly_packet: no chat id on file yet (owner hasn't messaged the bot); "
            "exiting cleanly",
            file=sys.stderr,
        )
        return 0

    report = weekly_packet.send_packet(token, chat_id, db_path=db_path)
    print(
        f"run_weekly_packet: run={report.iso_year}-W{report.iso_week:02d} "
        f"total={report.total_items} sent={report.items_sent} "
        f"predrafted={report.predrafted} degraded={report.degraded} cleared={report.cleared}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
