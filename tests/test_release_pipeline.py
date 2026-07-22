from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_tag_workflow_publishes_a_github_release_after_builds():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()

    assert "publish-release:" in workflow
    assert "needs: build-linux" in workflow
    assert "if: startsWith(github.ref, 'refs/tags/v')" in workflow
    assert 'gh release create "$GITHUB_REF_NAME"' in workflow
    assert "dist/appimage/* dist/deb/* dist/rpm/*" in workflow
    assert "--verify-tag" in workflow
