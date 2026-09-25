"""The generated sections of docs/REFERENCE.md: commands, configuration keys, run filters, HTTP API.

Each comes from the code itself, so the reference cannot drift from what the harness does:

    python -m omegaeval.docgen            rewrite the generated sections
    python -m omegaeval.docgen --check    exit 1 when a section is out of date (the test suite runs this)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REFERENCE = Path(__file__).resolve().parents[1] / "docs" / "REFERENCE.md"


def _usage(name: str, sp: argparse.ArgumentParser) -> str:
    parts = [f"omegaeval {name}"]
    positionals = [a for a in sp._actions if not a.option_strings]
    optionals = [a for a in sp._actions if a.option_strings and not isinstance(a, argparse._HelpAction)]
    for a in positionals:
        meta = a.metavar or a.dest
        if a.choices:
            meta = "|".join(a.choices)
        parts.append({"*": f"[{meta} ...]", "+": f"{meta} [{meta} ...]", "?": f"[{meta}]"}.get(a.nargs, meta))
    for a in optionals:
        flag = max(a.option_strings, key=len)
        if a.nargs == 0:
            parts.append(f"[{flag}]")
        else:
            meta = "|".join(a.choices) if a.choices else (a.metavar or a.dest.upper())
            parts.append(f"[{flag} {meta}]" + (" ..." if isinstance(a, argparse._AppendAction) else ""))
    return " ".join(parts)


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def cli_reference() -> str:
    from .cli import build_parser

    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    helps = {c.dest: c.help for c in sub._choices_actions}
    out = []
    for name, sp in sub.choices.items():
        out.append(f"### omegaeval {name}\n")
        h = helps.get(name, "").rstrip(".")
        out.append(f"{h[:1].upper() + h[1:]}.\n")
        out.append("```\n" + _usage(name, sp) + "\n```\n")
        rows = []
        actions = [a for a in sp._actions if not isinstance(a, argparse._HelpAction)]
        for a in sorted(actions, key=lambda a: bool(a.option_strings)):      # positionals first
            label = ", ".join(f"`{o}`" for o in a.option_strings) if a.option_strings else f"`{a.metavar or a.dest}`"
            default = "" if a.default in (None, "", False, []) or not a.option_strings else f"`{a.default}`"
            text = (a.help or "").rstrip(".")
            text = (text[:1].upper() + text[1:] + ".") if text else ""
            choices = f" One of: {', '.join(a.choices)}." if a.choices else ""
            rows.append(f"| {label} | {_cell(text)}{choices} | {default} |")
        if rows:
            out.append("| Argument | Meaning | Default |\n| --- | --- | --- |\n" + "\n".join(rows) + "\n")
    return "\n".join(out).strip() + "\n"


def _default(value, meaning: str) -> str:
    """A default as it would be written in TOML; descriptive defaults are in parentheses."""
    import json

    if value is None:
        return "required" if meaning.startswith("Required") else ""
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, (int, float)):
        return f"`{value}`"
    if isinstance(value, str) and value.startswith("("):
        return value
    if value in ({}, ""):
        return ""
    return f"`{json.dumps(value)}`"


def config_reference() -> str:
    from .config import SCHEMA

    out, tables = [], []
    for table, *_ in SCHEMA:
        if table not in tables:
            tables.append(table)
    for table in tables:
        out.append(f"#### [{table}]\n")
        out.append("| Key | Default | Meaning |\n| --- | --- | --- |")
        for t, key, default, meaning in SCHEMA:
            if t != table:
                continue
            shown = meaning[len("Required. "):] if meaning.startswith("Required. ") else meaning
            out.append(f"| `{key}` | {_cell(_default(default, meaning))} | {_cell(shown)} |")
        out.append("")
    return "\n".join(out).strip() + "\n"


def filter_reference() -> str:
    from .store import FILTER_KEYS

    return ("Filter keys: " + ", ".join(f"`{k}`" for k in FILTER_KEYS) +
            ". Values match exactly; `success`, `needs_judge`, `judged`, `void` and `exact` take true or false.\n")


def api_reference() -> str:
    from .server import API_ROUTES

    rows = [f"| {m} | `{p}` | {_cell(what)} |" for m, p, what in API_ROUTES]
    return "| Method | Path | Returns |\n| --- | --- | --- |\n" + "\n".join(rows) + "\n"


SECTIONS = {"cli": cli_reference, "config": config_reference, "filters": filter_reference, "api": api_reference}


def render(text: str) -> str:
    for name, make in SECTIONS.items():
        pattern = re.compile(rf"(<!-- generated:{name} -->\n).*?(<!-- /generated:{name} -->)", re.S)
        if not pattern.search(text):
            raise ValueError(f"REFERENCE.md has no generated:{name} section")
        body = make()
        text = pattern.sub(lambda m: m.group(1) + body + m.group(2), text)
    return text


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m omegaeval.docgen")
    p.add_argument("--check", action="store_true", help="exit 1 if docs/REFERENCE.md is out of date")
    p.add_argument("--file", default=str(REFERENCE))
    args = p.parse_args(argv)
    path = Path(args.file)
    current = path.read_text(encoding="utf-8")
    fresh = render(current)
    if args.check:
        if fresh != current:
            print(f"{path} is out of date: run python -m omegaeval.docgen", file=sys.stderr)
            return 1
        print(f"{path} is up to date")
        return 0
    if fresh != current:
        path.write_text(fresh, encoding="utf-8")
        print(f"updated {path}")
    else:
        print(f"{path} already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
