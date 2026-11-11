import re


class PatchError(Exception):
    pass


HDR = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse(diff):
    lines = diff.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    hunks, i = [], 0
    while i < len(lines) and not HDR.match(lines[i]):
        i += 1
    while i < len(lines):
        m = HDR.match(lines[i])
        if not m:
            raise PatchError("expected hunk")
        a = int(m.group(1)); b = int(m.group(2)) if m.group(2) is not None else 1
        d = int(m.group(4)) if m.group(4) is not None else 1
        i += 1
        body = []  # (kind, text, newline)
        old_n = new_n = 0
        while i < len(lines) and (old_n < b or new_n < d or (i < len(lines) and lines[i].startswith("\\"))):
            ln = lines[i]
            if ln.startswith("\\"):
                if not body:
                    raise PatchError("stray marker")
                k, t, _ = body[-1]
                body[-1] = (k, t, False)
                i += 1
                continue
            if ln == "":
                k, t = " ", ""
            elif ln[0] in " -+":
                k, t = ln[0], ln[1:]
            else:
                break
            body.append((k, t, True))
            if k in " -":
                old_n += 1
            if k in " +":
                new_n += 1
            i += 1
        if old_n != b or new_n != d:
            raise PatchError("count mismatch")
        old = [t + ("\n" if nl else "") for k, t, nl in body if k in " -"]
        new = [t + ("\n" if nl else "") for k, t, nl in body if k in " +"]
        hunks.append((a, b, old, new))
        if i < len(lines) and not HDR.match(lines[i]):
            raise PatchError("junk after hunk")
    return hunks


def apply_patch(original, diff):
    src = original.splitlines(keepends=True)
    out, cur = [], 0
    for a, b, old, new in parse(diff):
        exp = a if b == 0 else a - 1
        n = len(old)
        if n == 0:
            if exp < cur or exp > len(src):
                raise PatchError("insert out of range")
            pos = exp
        else:
            pos = None
            for dist in range(0, len(src) + abs(exp) + 1):
                for p in ([exp] if dist == 0 else [exp - dist, exp + dist]):
                    if cur <= p <= len(src) - n and src[p:p + n] == old:
                        pos = p
                        break
                if pos is not None:
                    break
            if pos is None:
                raise PatchError("hunk does not apply")
        out += src[cur:pos] + new
        cur = pos + n
    out += src[cur:]
    return "".join(out)
