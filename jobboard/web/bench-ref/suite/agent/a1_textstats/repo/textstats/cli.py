"""Command line interface."""
import argparse
import sys

from . import core


def build_parser():
    p = argparse.ArgumentParser(prog="textstats")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("count", help="most common words in a file")
    c.add_argument("file")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    with open(args.file, encoding="utf-8") as fh:
        text = fh.read()
    if args.command == "count":
        for word, n in core.top_words(text):
            print(f"{word}\t{n}")
    return 0
