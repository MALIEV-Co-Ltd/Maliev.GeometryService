from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def test_branch_workflows_delegate_to_read_only_validation() -> None:
    for name in (
        "ci-develop.yml",
        "ci-main.yml",
        "ci-staging.yml",
        "pr-validation.yml",
    ):
        workflow = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert "contents: read" in workflow
        assert "uses: ./.github/workflows/_validate.yml" in workflow


def test_validation_workflows_are_pinned_and_deployment_free() -> None:
    workflow_text = "\n".join(
        path.read_text(encoding="utf-8") for path in WORKFLOWS.glob("*.yml")
    )
    assert "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0" in workflow_text
    assert (
        "actions/setup-python@e797f83bcb11b83ae66e0230d6156d7c80228e7c" in workflow_text
    )
    assert (
        "snok/install-poetry@a783c322200f0519c7926aa6faa857c4e23e9263" in workflow_text
    )

    for forbidden in (
        "secrets.",
        "GCP_SA_KEY",
        "GITOPS_PAT",
        "google-github-actions/auth",
        "gcloud auth",
        "docker push",
        "maliev-gitops",
        "kustomize edit",
        "gh pr create",
        "pull_request_target",
    ):
        assert forbidden.casefold() not in workflow_text.casefold()


def test_dependabot_groups_supported_ecosystems() -> None:
    dependabot = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    for ecosystem in ("docker", "github-actions", "pip"):
        assert f"package-ecosystem: {ecosystem}" in dependabot
