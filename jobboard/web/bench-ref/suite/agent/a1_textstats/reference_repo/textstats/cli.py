"""Command line interface."""
import argparse
import json
import sys

from . import core


def _positive(v):
    n = int(v)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def build_parser():
    p = argparse.ArgumentParser(prog="textstats")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("count", "bigrams"):
        c = sub.add_parser(name)
        c.add_argument("file")
        c.add_argument("--top", type=_positive, default=10)
        c.add_argument("--ignore-case", action="store_true")
        c.add_argument("--json", action="store_true")
    s = sub.add_parser("stats")
    s.add_argument("file")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.file == "-":
            text = sys.stdin.read()
        else:
            with open(args.file, encoding="utf-8") as fh:
                text = fh.read()
    except OSError:
        print(f"error: cannot read {args.file}", file=sys.stderr)
        return 1
    if args.command == "stats":
        ws = core.words(text)
        print(f"words: {len(ws)}"); print(f"unique: {len(set(ws))}"); print(f"lines: {len(text.splitlines())}")
        return 0
    if args.command == "count":
        rows = core.top_words(text, args.top, args.ignore_case)
        items = [{"word": w, "count": n} for w, n in rows]
        plain = [f"{w}\t{n}" for w, n in rows]
    else:
        rows = core.top_bigrams(text, args.top, args.ignore_case)
        items = [{"bigram": f"{a} {b}", "count": n} for (a, b), n in rows]
        plain = [f"{a} {b}\t{n}" for (a, b), n in rows]
    if args.json:
        print(json.dumps(items))
    else:
        for line in plain:
            print(line)
    return 0
