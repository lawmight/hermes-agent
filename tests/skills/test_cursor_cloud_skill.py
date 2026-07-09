"""Smoke tests for the cursor-cloud optional skill.

We can't run real cloud agents in CI (needs CURSOR_API_KEY + network), so
these tests pin the contract: hardline frontmatter format, modern section
order, and that every `hermes cursor` verb the skill references actually
exists in the CLI parser.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest
import yaml

SKILL_DIR = (
    Path(__file__).resolve().parents[2]
    / "optional-skills" / "autonomous-ai-agents" / "cursor-cloud"
)


@pytest.fixture(scope="module")
def skill_src() -> str:
    return (SKILL_DIR / "SKILL.md").read_text()


@pytest.fixture(scope="module")
def frontmatter(skill_src) -> dict:
    m = re.search(r"^---\n(.*?)\n---", skill_src, re.DOTALL)
    assert m, "SKILL.md missing YAML frontmatter"
    return yaml.safe_load(m.group(1))


def test_skill_md_present() -> None:
    assert (SKILL_DIR / "SKILL.md").is_file()


def test_description_hardline(frontmatter) -> None:
    desc = frontmatter["description"]
    assert len(desc) <= 60, f"description is {len(desc)} chars (hardline ≤60)"
    assert desc.endswith("."), "description must end with a period"


def test_name_matches_dir(frontmatter) -> None:
    assert frontmatter["name"] == "cursor-cloud"


def test_modern_section_order(skill_src) -> None:
    sections = [
        "## When to Use",
        "## Prerequisites",
        "## How to Run",
        "## Quick Reference",
        "## Procedure",
        "## Pitfalls",
        "## Verification",
    ]
    positions = [skill_src.find(s) for s in sections]
    assert all(p >= 0 for p in positions), (
        f"missing sections: {[s for s, p in zip(sections, positions) if p < 0]}"
    )
    assert positions == sorted(positions), "sections out of hardline order"


def test_referenced_cli_verbs_exist(skill_src) -> None:
    """Every `hermes cursor <verb>` the skill mentions must be a real verb."""
    from hermes_cli.subcommands.cursor import build_cursor_parser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    build_cursor_parser(sub, cmd_cursor=lambda a: None)

    referenced = set(re.findall(r"hermes cursor ([a-z]+)", skill_src))
    # "archive|unarchive" style table rows split on the pipe already.
    for verb in sorted(referenced):
        # Parses without SystemExit → the verb exists (verbs w/ args get them).
        args_by_verb = {
            "launch": ["prompt"],
            "status": ["bc-1"], "follow": ["bc-1"], "cancel": ["bc-1"],
            "send": ["bc-1", "p"], "artifacts": ["bc-1"],
            "archive": ["bc-1"], "unarchive": ["bc-1"],
            "delete": ["bc-1"],
        }
        argv = ["cursor", verb] + args_by_verb.get(verb, [])
        parsed = parser.parse_args(argv)
        assert parsed.cursor_action == verb


def test_terminal_is_the_interaction_surface(skill_src) -> None:
    """Hardline rule 2: the prose points at the native `terminal` tool."""
    assert "`terminal`" in skill_src
