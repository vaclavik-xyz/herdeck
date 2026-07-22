from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_all_release_manifests_match_canonical_version():
    result = subprocess.run(
        [sys.executable, "scripts/set-version.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_release_workflow_rejects_a_tag_that_does_not_match_version():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()

    assert 'test "$GITHUB_REF_NAME" = "v$(cat VERSION)"' in workflow
