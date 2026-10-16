"""The Worker's clone of the repo: file operations confined to it, and the repo's checks.

Formatters run before checks, so the model isn't sent back for formatting alone.
"""

import subprocess
from pathlib import Path

OFF_LIMITS = {".git", ".github"}
MAX_OUTPUT_CHARS = 4000  # tail of each failing check's output

SETUP = [["uv", "sync", "--frozen"], ["npm", "--prefix", "dashboard", "ci"]]
FORMATTERS = [
    ["uv", "run", "ruff", "check", "--fix", "."],
    ["uv", "run", "ruff", "format", "."],
    ["npm", "--prefix", "dashboard", "run", "format"],
]
CHECKS = [
    ["uv", "run", "ruff", "check", "."],
    ["uv", "run", "mypy"],
    ["uv", "run", "pytest", "-q"],
    ["npm", "--prefix", "dashboard", "run", "lint"],
    ["npm", "--prefix", "dashboard", "run", "typecheck"],
    ["npm", "--prefix", "dashboard", "test"],
]


class Workspace:
    def __init__(self, *, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, path: str, *, writing: bool) -> Path:
        resolved = (self.root / path).resolve()
        if not resolved.is_relative_to(self.root) or resolved == self.root:
            raise ValueError(f"{path} is outside the repository")
        if writing and resolved.relative_to(self.root).parts[0] in OFF_LIMITS:
            raise ValueError(f"{path}: .git and .github are off limits")
        return resolved

    def read(self, *, path: str) -> str:
        return self._path(path, writing=False).read_text()

    def write(self, *, path: str, content: str) -> None:
        target = self._path(path, writing=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def replace(self, *, path: str, old: str, new: str) -> None:
        text = self.read(path=path)
        if (count := text.count(old)) != 1:
            raise ValueError(f"{path}: text to replace found {count} times, needs exactly 1")
        self.write(path=path, content=text.replace(old, new))

    def list_files(self) -> list[str]:
        return self.git("ls-files", "--cached", "--others", "--exclude-standard").splitlines()

    def search(self, *, pattern: str) -> str:
        found = self._run(["git", "grep", "-n", "-I", "-e", pattern, "--untracked"])
        return found.stdout[:MAX_OUTPUT_CHARS] or "No matches."

    def changed(self) -> bool:
        return bool(self.git("status", "--porcelain"))

    def git(self, *args: str) -> str:
        result = self._run(["git", *args])
        if result.returncode:
            raise RuntimeError(f"git {args[0]} failed: {result.stdout[-MAX_OUTPUT_CHARS:]}")
        return result.stdout

    def setup(self) -> None:
        for command in SETUP:
            result = self._run(command)
            if result.returncode:
                raise RuntimeError(f"{' '.join(command)} failed: {result.stdout[-1000:]}")

    def check(self) -> str:
        """Returns the failing checks' output ("" if all pass)."""
        for command in FORMATTERS:
            self._run(command)
        failures = []
        for command in CHECKS:
            result = self._run(command)
            if result.returncode:
                failures.append(f"$ {' '.join(command)}\n{result.stdout[-MAX_OUTPUT_CHARS:]}")
        return "\n\n".join(failures)

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed commands, no shell
            command,
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=900,
        )
