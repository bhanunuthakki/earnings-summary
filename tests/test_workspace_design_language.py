"""Guard the full brief's neutral, dense document language."""

from report.renderers.workspace_styles import CSS


def test_thesis_and_change_ledes_use_neutral_document_rules_not_color_rails() -> None:
    thesis_block = CSS.rsplit(".l1-thesis {", 1)[1].split("}", 1)[0]
    reread_block = CSS.rsplit(".l1-reread {", 1)[1].split("}", 1)[0]

    for block in (thesis_block, reread_block):
        assert "border-left" not in block
        assert "border-bottom: var(--bw-thin) solid var(--border)" in block
        assert "background: transparent" in block
