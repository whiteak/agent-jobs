"""Upgrade 7b: unified diff parser for the diff viewer (and review bundle)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

GENERATED_DIRS = ("build/", ".gradle/", "node_modules/", "dist/", "target/", "out/", ".idea/",
                  "__pycache__/", ".venv/", "bin/", "obj/", ".next/", "coverage/")
GENERATED_FILES = re.compile(r"(\.(apk|aab|jar|class|dex|so|o|a|exe|dll|png|jpe?g|gif|webp|ico|"
                             r"ttf|otf|woff2?|mp3|wav|flac|ogg|mp4|zip|gz|tar|7z|pdf|keystore|jks)$)|"
                             r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|Cargo\.lock|"
                             r"poetry\.lock|gradle-wrapper\.jar)$", re.I)


def is_generated(path: str) -> bool:
    p = path.lstrip("./")
    return any(p.startswith(d) or f"/{d}" in p for d in GENERATED_DIRS) or bool(GENERATED_FILES.search(p))


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    header: str
    lines: list = field(default_factory=list)   # (kind ' '|'+'|'-'|'\\', old_no, new_no, text)


@dataclass
class FileDiff:
    old_path: str
    new_path: str
    status: str = "modified"      # added | deleted | renamed | modified
    binary: bool = False
    hunks: list = field(default_factory=list)
    added: int = 0
    removed: int = 0

    @property
    def path(self) -> str:
        return self.new_path if self.status != "deleted" else self.old_path

    @property
    def generated(self) -> bool:
        return is_generated(self.path)


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def _strip(p: str) -> str:
    p = p.strip()
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse(diff: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    o = n = left_old = left_new = 0
    for line in diff.splitlines():
        in_hunk = hunk is not None and (left_old > 0 or left_new > 0)
        if in_hunk and line[:1] in (" ", "+", "-", ""):
            kind, text = (line[:1] or " "), line[1:]
            if kind == "+":
                hunk.lines.append(("+", None, n, text)); n += 1; left_new -= 1; cur.added += 1
            elif kind == "-":
                hunk.lines.append(("-", o, None, text)); o += 1; left_old -= 1; cur.removed += 1
            else:
                hunk.lines.append((" ", o, n, text)); o += 1; n += 1; left_old -= 1; left_new -= 1
            continue
        if line.startswith("\\") and hunk is not None:
            hunk.lines.append(("\\", None, None, line[1:].strip()))
            continue
        if line.startswith("diff --git "):
            m = re.match(r'^diff --git (".*?"|\S+) (".*?"|\S+)$', line)
            a, b = (_strip(m.group(1)), _strip(m.group(2))) if m else ("", "")
            cur = FileDiff(a, b)
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        if line.startswith("new file mode"):
            cur.status = "added"
        elif line.startswith("deleted file mode"):
            cur.status = "deleted"
        elif line.startswith("rename from "):
            cur.status = "renamed"; cur.old_path = line[12:]
        elif line.startswith("rename to "):
            cur.new_path = line[10:]
        elif line.startswith("Binary files ") or line == "GIT binary patch":
            cur.binary = True
        elif line.startswith("--- "):
            if line[4:].strip() != "/dev/null":
                cur.old_path = _strip(line[4:])
        elif line.startswith("+++ "):
            if line[4:].strip() != "/dev/null":
                cur.new_path = _strip(line[4:])
        else:
            m = HUNK_RE.match(line)
            if m:
                hunk = Hunk(int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1),
                            int(m.group(3)), int(m.group(4) if m.group(4) is not None else 1), m.group(5).strip())
                cur.hunks.append(hunk)
                o, n, left_old, left_new = hunk.old_start, hunk.new_start, hunk.old_len, hunk.new_len
    return files


def summary(files: list[FileDiff]) -> list[dict]:
    return [{"path": f.path, "old_path": f.old_path, "status": f.status, "added": f.added,
             "removed": f.removed, "binary": f.binary, "generated": f.generated} for f in files]


def side_by_side(h: Hunk) -> list[tuple]:
    """Pairs of (left, right) rows; each side is (line_no, kind, text) or None."""
    rows, dels, adds = [], [], []

    def flush():
        for i in range(max(len(dels), len(adds))):
            rows.append((dels[i] if i < len(dels) else None, adds[i] if i < len(adds) else None))
        dels.clear(); adds.clear()

    for kind, o, n, text in h.lines:
        if kind == "-":
            dels.append((o, "-", text))
        elif kind == "+":
            adds.append((n, "+", text))
        elif kind == " ":
            flush(); rows.append(((o, " ", text), (n, " ", text)))
    flush()
    return rows
