from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from .helpers import REPO_ROOT, WORKFLOWS, _git, _git_output


def _workflow(name: str) -> dict[str | bool, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _resolve_workflow_source(
    tmp_path: Path, workflow_name: str, identity: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    steps = _workflow(workflow_name)["jobs"]["instruction-hub"]["steps"]
    resolver = next(step for step in steps if step.get("id") == "toolchain-ref")
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-c", resolver["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            **resolver["env"],
            "JOB_WORKFLOW_REF": identity,
            # Caller identity must never select the compiler repository or ref.
            "GITHUB_REPOSITORY": "Customer/instruction-hub",
            "GITHUB_REF": "refs/heads/customer-change",
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, output.read_text() if output.exists() else ""


@pytest.mark.parametrize("workflow_name", ["pr-check.yml", "publish.yml"])
@pytest.mark.parametrize(
    ("repository", "ref"),
    [
        ("Promptless/pig-toolchain", "refs/heads/main"),
        ("Customer/internal-compiler", "refs/heads/releases/stable"),
        ("Customer/internal-compiler", "refs/tags/v1.2.3"),
        ("Customer/internal-compiler", "a" * 40),
        ("Customer/.github", "refs/pull/123/merge"),
        ("managed_user/toolchain.v2", "refs/heads/release@customer"),
    ],
)
def test_actual_workflow_resolver_uses_defining_repository_and_ref(
    tmp_path: Path, workflow_name: str, repository: str, ref: str
) -> None:
    result, output = _resolve_workflow_source(
        tmp_path, workflow_name, f"{repository}/.github/workflows/{workflow_name}@{ref}"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert output == f"repository={repository}\nref={ref}\n"


@pytest.mark.parametrize("workflow_name", ["pr-check.yml", "publish.yml"])
@pytest.mark.parametrize(
    "identity",
    [
        "",
        "Customer/toolchain/.github/workflows/other.yml@refs/heads/main",
        "Customer/toolchain/nested/.github/workflows/{workflow}@refs/heads/main",
        "Customer/toolchain/.github/workflows/{workflow}/extra@refs/heads/main",
        "Customer/toolchain/.github/workflows/{workflow}@",
        "Customer/toolchain/.github/workflows/{workflow}",
        "https://github.com/Customer/toolchain/.github/workflows/{workflow}@refs/heads/main",
        "Customer/../.github/workflows/{workflow}@refs/heads/main",
        "Customer/toolchain;touch/.github/workflows/{workflow}@refs/heads/main",
        "Customer/toolchain/.github/workflows/{workflow}@main",
        "Customer/toolchain/.github/workflows/{workflow}@-bmalicious",
        "Customer/toolchain/.github/workflows/{workflow}@1234abcd",
        "Customer/toolchain/.github/workflows/{workflow}@refs/heads/../main",
        "Customer/toolchain/.github/workflows/{workflow}@refs/heads/main:other",
        "Customer/toolchain/.github/workflows/{workflow}@refs/heads/main.lock",
        "Customer/toolchain/.github/workflows/{workflow}@refs/heads/main\nrepository=Attacker/compiler",
        "Customer/toolchain/.github/workflows/{workflow}@refs/heads/main\r",
        "Customer/toolchain/.github/workflows/{workflow}@refs/heads/main\t",
        "Customer/toolchain/.github/workflows/{workflow}@refs/heads/main\x7f",
        "Customer/toolchain/.github/workflows/{workflow}@refs/heads/main with spaces",
    ],
)
def test_actual_workflow_resolver_rejects_invalid_identity_before_writing_outputs(
    tmp_path: Path, workflow_name: str, identity: str
) -> None:
    result, output = _resolve_workflow_source(tmp_path, workflow_name, identity.format(workflow=workflow_name))

    assert result.returncode == 2, result.stdout + result.stderr
    assert "workflow" in result.stderr
    assert output == ""


@pytest.mark.parametrize("workflow_name", ["pr-check.yml", "publish.yml"])
def test_private_toolchain_token_only_authenticates_resolved_compiler_checkout(workflow_name: str) -> None:
    workflow = _workflow(workflow_name)
    # PyYAML's YAML 1.1 parser treats the GitHub Actions `on` key as True.
    call = workflow[True]["workflow_call"]
    assert call["secrets"]["toolchain-token"]["required"] is False
    assert "toolchain-repository" not in call["inputs"]
    assert "toolchain-ref" not in call["inputs"]

    steps = workflow["jobs"]["instruction-hub"]["steps"]
    caller_checkout, compiler_checkout = [
        step for step in steps if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert "token" not in caller_checkout["with"]
    assert "repository" not in caller_checkout["with"]
    assert compiler_checkout["with"] == {
        "repository": "${{ steps.toolchain-ref.outputs.repository }}",
        "ref": "${{ steps.toolchain-ref.outputs.ref }}",
        "token": "${{ secrets.toolchain-token || github.token }}",
        "path": ".promptless-pig-toolchain",
        "persist-credentials": False,
    }
    assert steps[-1]["uses"] == "./.promptless-pig-toolchain"
    assert "toolchain-token" not in str(steps[-1])


def _render_gitlab_bootstrap(inputs: dict[str, str]) -> tuple[str, dict[str, str]]:
    text = (REPO_ROOT / "templates/gitlab/instruction-hub.yml").read_text()
    spec, _ = yaml.safe_load_all(text)
    for name, definition in spec["spec"]["inputs"].items():
        value = inputs.get(name, definition["default"])
        if "regex" in definition and re.fullmatch(definition["regex"], value) is None:
            raise ValueError(f"Invalid template input: {name}")
        text = text.replace(f"$[[ inputs.{name} ]]", value)
    _, template = yaml.safe_load_all(text)
    setup = template[".instruction-hub"]
    # Exercise the actual compiler checkout without reinstalling uv in this test.
    commands = [command for command in setup["before_script"] if not command.startswith("python -m pip install ")]
    return "\n".join(commands), setup["variables"]


@pytest.mark.parametrize("repository", [None, "https://git.example.com/customer/platform/pig-toolchain.git"])
@pytest.mark.parametrize("pinned", [False, True])
def test_gitlab_bootstrap_fetches_configured_repository_and_ref(
    tmp_path: Path, repository: str | None, pinned: bool
) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "--initial-branch=main")
    _git(upstream, "config", "user.name", "Test")
    _git(upstream, "config", "user.email", "test@example.com")
    (upstream / "compiler-version").write_text("first")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", "first compiler")
    pinned_sha = _git_output(upstream, "rev-parse", "HEAD").strip()
    (upstream / "compiler-version").write_text("latest")
    _git(upstream, "commit", "-am", "latest compiler")
    latest_sha = _git_output(upstream, "rev-parse", "HEAD").strip()
    inputs = {"toolchain-repository": repository} if repository else {}
    if pinned:
        inputs["toolchain-ref"] = pinned_sha
    bootstrap, variables = _render_gitlab_bootstrap(inputs)
    expected_url = repository or "https://github.com/Promptless/pig-toolchain.git"
    checked_out = tmp_path / "checked-out-path"
    result = subprocess.run(
        [
            "bash",
            "-euo",
            "pipefail",
            "-c",
            bootstrap + '\nprintf "%s" "$INSTRUCTION_HUB_TOOLCHAIN_DIR" > "$CHECKED_OUT"',
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            **variables,
            "CHECKED_OUT": str(checked_out),
            "TMPDIR": str(tmp_path),
            # Keep the configured HTTPS origin intact while fetching real Git data
            # locally; this test does not depend on network access or mirror hosting.
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{upstream.as_uri()}.insteadOf",
            "GIT_CONFIG_VALUE_0": expected_url,
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    checkout = Path(checked_out.read_text())
    expected_sha = pinned_sha if pinned else latest_sha
    assert _git_output(checkout, "rev-parse", "HEAD").strip() == expected_sha
    assert _git_output(checkout, "config", "remote.origin.url").strip() == expected_url
    assert (checkout / "compiler-version").read_text() == ("first" if pinned else "latest")
    assert f"Toolchain commit {expected_sha}" in result.stdout


@pytest.mark.parametrize(
    "repository",
    [
        "",
        "http://git.example.com/customer/compiler.git",
        "file:///tmp/compiler",
        "git@git.example.com:customer/compiler.git",
        "https://token@git.example.com/customer/compiler.git",
        "https://user:token@git.example.com/customer/compiler.git",
        "https://git.example.com/customer/compiler.git?token=secret",
        "https://git.example.com/customer/compiler.git#branch",
        "https://git.example.com/customer/compiler.git\n",
        "https://git.example.com/customer/compiler.git'$(touch OWNED)",
        "https://git.example.com/customer/$COMPILER.git",
        "https://git.example.com/customer/../compiler.git",
        "https://git.example.com/customer//compiler.git",
        "https://git.example.com/customer/compiler.git/",
    ],
)
def test_gitlab_toolchain_repository_input_rejects_credentials_and_noncanonical_urls(repository: str) -> None:
    with pytest.raises(ValueError, match="Invalid template input: toolchain-repository"):
        _render_gitlab_bootstrap({"toolchain-repository": repository})
