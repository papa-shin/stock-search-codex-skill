from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ValidationDependencyLockTest(unittest.TestCase):
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
