# -*- coding: utf-8 -*-
"""Skills runtime unit tests (DSH 借鉴 Phase 3)."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity.skills import list_skills, load_skill, match_skills, _parse_frontmatter


class TestFrontmatter:
    def test_parse(self):
        meta = _parse_frontmatter("---\nname: x\ndescription: y\nwhen_to_use: z\n---\nbody")
        assert meta == {"name": "x", "description": "y", "when_to_use": "z"}

    def test_no_frontmatter(self):
        assert _parse_frontmatter("plain text") == {}


class TestSkills:
    def test_list_has_skills(self):
        skills = list_skills()
        names = [s["name"] for s in skills]
        assert len(skills) >= 5
        assert "trinity-corrections" in names
        assert "trinity-skill-index" in names

    def test_load_skill(self):
        s = load_skill("trinity-corrections")
        assert s is not None
        assert s["description"]
        assert s["content"].strip()

    def test_load_by_filename(self):
        s = load_skill("corrections")
        assert s is not None

    def test_load_missing(self):
        assert load_skill("no-such-skill") is None

    def test_match(self):
        hits = match_skills("记忆修正 错误", top_k=3)
        assert hits, "should match corrections skill"
        names = [h["name"] for h in hits]
        assert "trinity-corrections" in names
