import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from worker.workspace import CHECKS, Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    workspace = Workspace(root=tmp_path)
    workspace.git("init", "-q")
    (tmp_path / "app.py").write_text("print('hi')\n")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "ci.yml").write_text("on: push\n")
    return workspace


def test_reads_writes_and_lists_files_inside_the_repo(workspace: Workspace) -> None:
    workspace.write(path="src/new.py", content="x = 1\n")

    assert workspace.read(path="src/new.py") == "x = 1\n"
    assert set(workspace.list_files()) >= {"app.py", "src/new.py", ".github/ci.yml"}


@pytest.mark.parametrize("path", ["../outside.py", "/etc/passwd", "src/../../outside.py"])
def test_refuses_paths_outside_the_repo(workspace: Workspace, path: str) -> None:
    with pytest.raises(ValueError, match="outside the repository"):
        workspace.write(path=path, content="x")
    with pytest.raises(ValueError, match="outside the repository"):
        workspace.read(path=path)


@pytest.mark.parametrize("path", [".github/ci.yml", ".git/config"])
def test_refuses_to_change_ci_or_git(workspace: Workspace, path: str) -> None:
    with pytest.raises(ValueError, match="off limits"):
        workspace.write(path=path, content="x")


def test_replace_needs_exactly_one_match(workspace: Workspace) -> None:
    workspace.replace(path="app.py", old="'hi'", new="'hello'")
    assert workspace.read(path="app.py") == "print('hello')\n"

    with pytest.raises(ValueError, match="found 0 times"):
        workspace.replace(path="app.py", old="missing", new="x")


def test_search_finds_lines(workspace: Workspace) -> None:
    assert "app.py:1:print('hi')" in workspace.search(pattern="print")


def test_changed_sees_new_files(workspace: Workspace) -> None:
    workspace.git("add", "-A")
    workspace.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    assert not workspace.changed()

    workspace.write(path="new.py", content="")

    assert workspace.changed()


def test_check_formats_then_reports_only_failing_checks(workspace: Workspace) -> None:
    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        failing = command == CHECKS[-1]
        return subprocess.CompletedProcess(
            command, returncode=1 if failing else 0, stdout="1 test failed" if failing else "ok"
        )

    with patch("worker.workspace.subprocess.run", side_effect=run) as subprocess_run:
        failures = workspace.check()

    assert failures == f"$ {' '.join(CHECKS[-1])}\n1 test failed"
    commands = [call.args[0] for call in subprocess_run.call_args_list]
    assert commands[-len(CHECKS) :] == CHECKS


def test_check_is_empty_when_everything_passes(workspace: Workspace) -> None:
    passing = subprocess.CompletedProcess([], returncode=0, stdout="ok")
    with patch("worker.workspace.subprocess.run", return_value=passing):
        assert workspace.check() == ""
