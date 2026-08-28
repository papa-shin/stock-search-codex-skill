from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ValidationDependencyLockTest(unittest.TestCase):
    def test_ci_actions_are_immutable_node24_releases(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertEqual(
            workflow.count(
                "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803"
            ),
            2,
        )
        self.assertEqual(
            workflow.count(
                "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1"
            ),
            2,
        )

    def test_pyyaml_validation_dependency_is_binary_hash_locked(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        requirements = (
            ROOT / ".github" / "requirements-validation.txt"
        ).read_text(encoding="utf-8")
        tests_job, validation_job = workflow.split("  validate-skill:", 1)

        self.assertNotIn("requirements-validation.txt", tests_job)
        self.assertIn("--require-hashes", validation_job)
        self.assertIn("--only-binary=:all:", validation_job)
        self.assertIn("--no-deps", validation_job)
        self.assertIn(
            "--requirement .github/requirements-validation.txt",
            validation_job,
        )
        self.assertEqual(
            requirements.strip(),
            "PyYAML==6.0.2 "
            "--hash=sha256:80bab7bfc629882493af4aa31a4cfa43a4c57c83813253626916b8c7ada83476",
        )


if __name__ == "__main__":
    unittest.main()
