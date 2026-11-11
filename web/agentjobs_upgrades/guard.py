"""Upgrade 1b: cheating guard.

Takes a snapshot of the project's test files before fix rounds, and after
each round reports if tests were deleted, skip markers were added, the test
count dropped, or check/test config was edited.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

IGNORE_DIRS = {".git", "node_modules", "build", ".gradle", "dist", "target", "__pycache__",
               ".venv", "venv", ".idea", "out", "bin", "obj"}

TEST_FILE_RE = re.compile(
    r"(^|/)(test_[^/]*\.py|[^/]*_test\.py|[^/]*\.(test|spec)\.(js|jsx|ts|tsx|mjs|cjs)"
    r"|[^/]*Test(s)?\.(kt|java|cs)|[^/]*_test\.(go|rs))$")
TEST_DIR_RE = re.compile(r"(^|/)(tests?|__tests__|src/test|src/androidTest)/")

# Files whose change during a fix round is suspicious. package.json is only
# judged on its test script / jest section (adding a dependency is normal).
CONFIG_FILES = {"AGENT_CHECKS.json", "pytest.ini", "conftest.py", "tox.ini",
                "jest.config.js", "jest.config.ts", "jest.config.cjs", "vitest.config.ts",
                "vitest.config.js", "package.json"}


def _config_fingerprint(rel: str, data: bytes) -> str:
    if rel == "package.json":
        import json
        try:
            j = json.loads(data.decode("utf-8"))
            data = json.dumps({"test": (j.get("scripts") or {}).get("test"), "jest": j.get("jest")},
                              sort_keys=True).encode()
        except ValueError:
            pass
    return hashlib.sha256(data).hexdigest()

SKIP_PATTERNS = [
    r"@Ignore\b", r"@Disabled\b", r"@pytest\.mark\.skip", r"@pytest\.mark\.xfail",
    r"@unittest\.skip", r"\bunittest\.skip\w*\(", r"\bself\.skipTest\(", r"\bpytest\.skip\(",
    r"\b(it|test|describe)\.skip\(", r"\bx(it|describe|test)\(", r"\b(it|test|describe)\.todo\(",
    r"#\[ignore\]", r"\[Ignore\]", r"\[Fact\(Skip\s*=", r"\bt\.Skip\(",
]
SKIP_RE = re.compile("|".join(SKIP_PATTERNS))
EARLY_RETURN_RE = re.compile(
    r"(def test\w*\([^)]*\)\s*(->\s*\w+\s*)?:\s*\n\s+return\b)"
    r"|(fun `?\w[^(]*\([^)]*\)\s*(\{)\s*\n\s*return\b)"
    r"|((it|test)\(\s*['\"][^'\"]*['\"]\s*,\s*(async\s*)?\(\)\s*=>\s*\{\s*\n?\s*return\s*;?)")


def is_test_file(rel: str) -> bool:
    return bool(TEST_FILE_RE.search(rel) or (TEST_DIR_RE.search(rel) and
                                             re.search(r"\.(py|js|ts|tsx|kt|java|cs|go|rs)$", rel)))


def _walk(project: Path):
    for p in project.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(project).as_posix()
        if any(part in IGNORE_DIRS for part in rel.split("/")[:-1]):
            continue
        yield rel, p


@dataclass
class TestSnapshot:
    files: dict = field(default_factory=dict)       # rel -> sha256
    skips: dict = field(default_factory=dict)       # rel -> count of skip markers
    early_returns: dict = field(default_factory=dict)
    configs: dict = field(default_factory=dict)     # rel -> sha256
    test_count: Optional[int] = None

    def to_dict(self):
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


def snapshot(project: Path, test_count: Optional[int] = None) -> TestSnapshot:
    project = Path(project)
    snap = TestSnapshot(test_count=test_count)
    for rel, p in _walk(project):
        if rel in CONFIG_FILES:
            snap.configs[rel] = _config_fingerprint(rel, p.read_bytes())
        if is_test_file(rel):
            data = p.read_bytes()
            snap.files[rel] = hashlib.sha256(data).hexdigest()
            text = data.decode("utf-8", errors="replace")
            snap.skips[rel] = len(SKIP_RE.findall(text))
            snap.early_returns[rel] = len(EARLY_RETURN_RE.findall(text))
    return snap


def compare(before: TestSnapshot, after: TestSnapshot) -> list[str]:
    """Human readable list of suspicious changes. Empty list = clean."""
    issues = []
    for rel in sorted(set(before.files) - set(after.files)):
        issues.append(f"Test file deleted: {rel}")
    for rel, n in sorted(after.skips.items()):
        old = before.skips.get(rel, 0)
        if n > old:
            issues.append(f"Skip/ignore marker added in {rel} ({old} → {n})")
    for rel, n in sorted(after.early_returns.items()):
        old = before.early_returns.get(rel, 0)
        if n > old:
            issues.append(f"Test made to return early in {rel}")
    for rel in sorted(set(before.configs) | set(after.configs)):
        if before.configs.get(rel) != after.configs.get(rel):
            what = "deleted" if rel not in after.configs else ("added" if rel not in before.configs else "changed")
            issues.append(f"Check/test config {what}: {rel}")
    if before.test_count is not None and after.test_count is not None and after.test_count < before.test_count:
        issues.append(f"Number of tests dropped ({before.test_count} → {after.test_count})")
    return issues
