"""One-command revert (+ optional lock) for a model or prompt auto-switch —
the §10 Q3 remediation affordance.

RISKY purposes CAN auto-switch (owner decision: higher bars, no frozenset), so
the counterweight is fast, obvious remediation:

    # Revert a model auto-switch (deactivate the override; code pin resumes):
    python execution/revert_model_switch.py --purpose bear_case --repo-root <MAIN>

    # Revert AND lock: pin the purpose to a model with a manual row the auto
    # loop will never overwrite (apply_model_switches skips set_by='manual*'):
    python execution/revert_model_switch.py --purpose bear_case \\
        --lock-model claude-sonnet-4-6 --repo-root <MAIN>

    # Revert an auto-applied PROMPT override (§10 Q1):
    python execution/revert_model_switch.py --purpose bear_case --prompt \\
        --repo-root <MAIN>

Unlock = plain revert (deactivates the manual row too). All rows are kept
inactive as the audit trail.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

log = logging.getLogger("revert_model_switch")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--purpose", required=True)
    parser.add_argument(
        "--prompt",
        action="store_true",
        help="revert the PROMPT override (prompt_pin_overrides) instead of the model pin",
    )
    parser.add_argument(
        "--lock-model",
        default=None,
        help="after reverting, write a manual model pin the auto loop must respect",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db_path = args.repo_root.resolve() / "data" / "portfolio.db"
    if not db_path.exists():
        log.error("DB not found at %s", db_path)
        return 1

    if args.prompt:
        from llm.prompt_ab import deactivate_prompt_override

        if deactivate_prompt_override(args.purpose, db_path=db_path):
            log.info("prompt override for %r deactivated (history kept)", args.purpose)
        else:
            log.info("no active prompt override for %r", args.purpose)
        return 0

    from llm.model_overrides import deactivate_override, write_pin_override

    if deactivate_override(args.purpose, db_path=db_path):
        log.info("model override for %r deactivated — code pin resumes", args.purpose)
    else:
        log.info("no active model override for %r", args.purpose)

    if args.lock_model:
        write_pin_override(
            args.purpose,
            args.lock_model,
            set_by="manual:lock",
            reason={
                "note": "operator lock via revert_model_switch.py — auto loop must not overwrite"
            },
            db_path=db_path,
        )
        log.info(
            "LOCKED %r -> %s (set_by=manual:lock; apply_model_switches will skip "
            "this purpose until the lock is deactivated)",
            args.purpose,
            args.lock_model,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
