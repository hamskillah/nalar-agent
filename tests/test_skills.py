import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from nalar.skills import SkillError, check, discover, prompt_block, use_skill

ROOT = Path(__file__).resolve().parents[1]


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def skill(self, name, body, description="A test skill.", text=None):
        folder = self.root / name
        folder.mkdir(exist_ok=True)
        (folder / "SKILL.md").write_text(
            text if text is not None else f"---\nname: {name}\ndescription: {description}\n---\n{body}",
            encoding="utf-8")
        return folder

    def test_discovers_valid_skill_and_ignores_invalid(self):
        self.skill("review", "Review the diff.\n")
        self.skill("Bad Name", "nope\n")
        (self.root / "plain").mkdir()
        (self.root / "plain/SKILL.md").write_text("no frontmatter", encoding="utf-8")
        skills = discover([self.root])
        self.assertEqual([skill.name for skill in skills], ["review"])
        self.assertIn("A test skill.", prompt_block(skills))

    def test_late_valid_skills_survive_the_directory_cap(self):
        for index in range(64):
            (self.root / f"Bad{index}").mkdir()
        folder = self.root / "zzz"
        folder.mkdir()
        (folder / "SKILL.md").write_text(
            "---\nname: zzz\ndescription: late valid skill\n---\nBody.\n", encoding="utf-8")
        self.assertEqual([skill.name for skill in discover([self.root])], ["zzz"])

    def test_use_skill_returns_body_and_rejects_unknown(self):
        self.skill("review", "Read the diff first.\n")
        self.assertEqual(use_skill(discover([self.root]), "review"), "Read the diff first.\n")
        with self.assertRaises(SkillError):
            use_skill(discover([self.root]), "missing")

    def test_rejects_oversized_and_mismatched_skills(self):
        self.skill("huge", "x" * 20_001)
        self.skill("mismatch", "body", text="---\nname: other\ndescription: d\n---\nbody")
        self.assertEqual(discover([self.root]), [])

    def test_check_reports_why_skills_are_rejected(self):
        self.skill("review", "body")
        self.skill("mismatch", "body",
                   text="---\nname: other\ndescription: d\n---\nbody")
        (self.root / "empty").mkdir()
        (self.root / "Bad_Name").mkdir()
        found, problems = check([self.root])
        self.assertEqual([item.name for item in found], ["review"])
        reasons = " ".join(item["reason"] for item in problems)
        self.assertIn("does not match", reasons)
        self.assertIn("SKILL.md is missing", reasons)
        self.assertIn("invalid skill folder name", reasons)

    def test_check_reports_symlinked_skill_folders(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text(
            "---\nname: linked\ndescription: d\n---\nbody\n",
            encoding="utf-8")
        (self.root / "link").symlink_to(outside, target_is_directory=True)
        found, problems = check([self.root])
        self.assertEqual(found, [])
        self.assertIn("skill folder is a symlink",
                      " ".join(item["reason"] for item in problems))

    def test_check_reports_missing_and_symlinked_directories(self):
        missing = self.root / "missing"
        found, problems = check([missing])
        self.assertEqual(found, [])
        self.assertIn("missing", problems[0]["reason"])
        real = self.root / "real"
        real.mkdir()
        link = self.root / "link"
        link.symlink_to(real, target_is_directory=True)
        found, problems = check([link])
        self.assertEqual(found, [])
        self.assertIn("symlink", problems[0]["reason"])

    def test_cli_check_reports_problems_and_exit_codes(self):
        skills = self.root / ".nalar" / "skills"
        review = skills / "review"
        review.mkdir(parents=True)
        (review / "SKILL.md").write_text(
            "---\nname: review\ndescription: Review\n---\nbody\n",
            encoding="utf-8")
        env = {**os.environ, "NALAR_GLOBAL_CONFIG": str(self.root / "none.json")}
        command = [sys.executable, "-m", "nalar", "skills", "check",
                   "--repo", str(self.root), "--format", "json"]
        clean = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                               text=True)
        self.assertEqual(clean.returncode, 0, clean.stderr)
        payload = json.loads(clean.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual([item["name"] for item in payload["skills"]], ["review"])

        broken = skills / "broken"
        broken.mkdir()
        (broken / "SKILL.md").write_text(
            "---\nname: other\ndescription: d\n---\nbody\n",
            encoding="utf-8")
        found = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                               text=True)
        self.assertEqual(found.returncode, 1, found.stderr)
        payload = json.loads(found.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("does not match", payload["problems"][0]["reason"])
        text = subprocess.run(command[:-2] + ["--format", "text"], cwd=ROOT,
                              env=env, capture_output=True, text=True)
        self.assertEqual(text.returncode, 1)
        self.assertIn("problem(s)", text.stdout)

    def test_prompt_block_marks_skills_as_untrusted_guidance(self):
        self.skill("review", "body")
        block = prompt_block(discover([self.root]))
        self.assertIn("untrusted", block.lower())
        self.assertIn("use_skill", block)
