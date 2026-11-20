#!/usr/bin/env python3
"""Pick the local engines GCI benchmarks for a change, like `nx affected`.

Pull requests run the engines their diff touches (see engine-impact.json);
anything shared, unclassified or ambiguous runs every local engine, as does
every other event (pushes to main, manual dispatch) and any selector error.

Run from the repository root:
    python3 tools/scripts/github/select-engines.py --event pull_request --base <sha> --head <sha>

Writes `mode` (full | subset | none) and `engines` (comma-separated) to
$GITHUB_OUTPUT and a reason table to $GITHUB_STEP_SUMMARY when they are set.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

REPO = Path(__file__).resolve().parents[3]
MANIFEST = Path(__file__).resolve().with_name("engine-impact.json")
CATALOG_DIR = "src/containers/sql_catalog"
MAX_SUMMARY_ROWS = 200


@dataclass
class Selection:
    mode: str
    engines: List[str]
    reasons: List[Tuple[str, str]] = field(default_factory=list)


@lru_cache(maxsize=None)
def _glob(pattern: str) -> "re.Pattern[str]":
    regex, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            regex, i = regex + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            regex, i = regex + ".*", i + 2
        elif pattern[i] == "*":
            regex, i = regex + "[^/]*", i + 1
        elif pattern[i] == "?":
            regex, i = regex + "[^/]", i + 1
        else:
            regex, i = regex + re.escape(pattern[i]), i + 1
    return re.compile(regex + r"\Z")


def matches(path: str, patterns: Iterable[str]) -> bool:
    return any(_glob(pattern).match(path) for pattern in patterns)


def load_manifest(path: Path = MANIFEST) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    engines = manifest["engines"]
    for name, engine in engines.items():
        if engine["kind"] not in {"local", "cloud", "framework"}:
            raise ValueError(f"{name}: unknown kind {engine['kind']!r}")
        for partner in engine["depends_on"]:
            if partner not in engines:
                raise ValueError(f"{name}: depends_on unknown engine {partner!r}")
    for rule in manifest["rules"]:
        for name in rule["engines"]:
            if name not in engines:
                raise ValueError(f"rule names unknown engine {name!r}")
    if not 0 < manifest["full_suite_fraction"] < 1:
        raise ValueError("full_suite_fraction must be between 0 and 1")
    return manifest


def local_engines(manifest: dict) -> List[str]:
    return [name for name, engine in manifest["engines"].items() if engine["kind"] == "local"]


def classify(path: str, manifest: dict) -> Tuple[str, Set[str]]:
    """Return (rule kind, engines) for one path; catalog data is resolved later."""
    if matches(path, manifest["no_effect"]):
        return "no_effect", set()
    if matches(path, manifest["sql_catalog"]):
        return "sql_catalog", set()
    engines = set()
    for rule in manifest["rules"]:
        if matches(path, rule["paths"]):
            engines.update(rule["engines"])
    if engines:
        return "engine", engines
    if matches(path, manifest["global"]):
        return "global", set()
    return "unclassified", set()


def _with_partners(engines: Set[str], manifest: dict) -> Set[str]:
    pending, closed = list(engines), set()
    while pending:
        name = pending.pop()
        if name not in closed:
            closed.add(name)
            pending.extend(manifest["engines"][name]["depends_on"])
    return closed


def full(manifest: dict, reasons: List[Tuple[str, str]]) -> Selection:
    return Selection("full", local_engines(manifest), reasons)


def select(
    paths: Sequence[str],
    manifest: dict,
    catalog_engines: Callable[[], Set[str]],
) -> Selection:
    """Choose engines for a pull request that changed `paths`.

    `catalog_engines` is only called when SQL catalog data changed; it returns
    every catalog engine whose rendered SQL differs between base and head.
    """
    reasons: List[Tuple[str, str]] = []
    selected: Set[str] = set()
    run_all = False
    catalog_paths = []
    for path in sorted(set(paths)):
        kind, engines = classify(path, manifest)
        if kind == "no_effect":
            reasons.append((path, "no effect"))
        elif kind == "sql_catalog":
            catalog_paths.append(path)
        elif kind == "engine":
            selected |= engines
            reasons.append((path, ", ".join(sorted(engines))))
        else:
            run_all = True
            reasons.append((path, f"{kind}: every local engine"))
    if catalog_paths:
        rendered = catalog_engines()
        unknown = rendered - set(manifest["engines"])
        framework = {e for e in rendered & set(manifest["engines"])
                     if manifest["engines"][e]["kind"] == "framework"}
        if unknown or framework:
            run_all = True
        selected |= rendered - unknown - framework
        effect = ", ".join(sorted(rendered)) or "no rendered SQL changed"
        if unknown or framework:
            effect += ": every local engine"
        reasons.extend((path, f"sql catalog render diff: {effect}") for path in catalog_paths)
    local = local_engines(manifest)
    chosen = [name for name in local if name in _with_partners(selected, manifest)]
    if run_all:
        return full(manifest, reasons)
    if len(chosen) > manifest["full_suite_fraction"] * len(local):
        reasons.append(("(selection)", f"{len(chosen)} of {len(local)} local engines: every local engine"))
        return full(manifest, reasons)
    return Selection("subset" if chosen else "none", chosen, reasons)


def _git(*args: str, repo: Path = REPO) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def changed_paths(base: str, head: str, repo: Path = REPO) -> List[str]:
    """Old and new path of every file changed since the merge base."""
    merge_base = _git("merge-base", base, head, repo=repo).strip()
    fields = _git("diff", "--name-status", "-z", "-M", merge_base, head, repo=repo).split("\0")
    paths, i = [], 0
    while i < len(fields) and fields[i]:
        status = fields[i]
        count = 2 if status[0] in "RC" else 1
        paths.extend(fields[i + 1:i + 1 + count])
        i += 1 + count
    return paths


def render_dump(containers: Path) -> Dict[str, Dict[str, str]]:
    """Hash every catalog entry rendered for every catalog engine."""
    sys.path.insert(0, str(containers))
    from sql_catalog import generator

    dump = {}
    targets = generator._project_targets()
    skips = generator._skips()
    for engine in generator._load_engines():
        rendered = {}
        for category, name in generator._entry_names():
            key = (engine, category, name)
            if category != "inline" and key not in targets:
                continue
            if key in skips:
                continue
            if category == "inline" and not (
                generator._catalog_path(category, name).is_file()
                or generator._override_path(engine, category, name).is_file()
            ):
                continue
            text = generator.render_sql(engine, category, name)
            rendered[f"{category}/{name}"] = hashlib.sha256(text.encode()).hexdigest()
        dump[engine] = rendered
    return dump


def _render_at(sha: str, repo: Path) -> Dict[str, Dict[str, str]]:
    archive = subprocess.run(
        ["git", "archive", "--format=tar", sha, CATALOG_DIR],
        cwd=repo, check=True, capture_output=True,
    ).stdout
    with tempfile.TemporaryDirectory() as directory:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(directory, filter="data")
            else:
                tar.extractall(directory)
        containers = Path(directory) / CATALOG_DIR / ".."
        output = subprocess.run(
            [sys.executable, __file__, "--render-dump", str(containers.resolve())],
            check=True, capture_output=True, text=True,
        ).stdout
    return json.loads(output)


def rendered_engine_diff(base: str, head: str, repo: Path = REPO) -> Set[str]:
    merge_base = _git("merge-base", base, head, repo=repo).strip()
    with ThreadPoolExecutor(max_workers=2) as pool:
        before, after = pool.map(lambda sha: _render_at(sha, repo), (merge_base, head))
    return {engine for engine in set(before) | set(after) if before.get(engine) != after.get(engine)}


def resolve(args: argparse.Namespace, manifest_path: Path = MANIFEST, repo: Path = REPO) -> Selection:
    try:
        manifest = load_manifest(manifest_path)
    except Exception as error:
        return Selection("full", [], [("(manifest)", f"unreadable, every local engine: {error}")])
    try:
        if args.force_full:
            return full(manifest, [("(forced)", "every local engine")])
        if args.event != "pull_request":
            return full(manifest, [(f"({args.event})", "every local engine")])
        if not args.base or not args.head:
            raise ValueError("pull requests need --base and --head")
        paths = changed_paths(args.base, args.head, repo)
        return select(paths, manifest, lambda: rendered_engine_diff(args.base, args.head, repo))
    except Exception as error:
        return full(manifest, [("(selector error)", f"every local engine: {type(error).__name__}: {error}")])


def summary(selection: Selection) -> str:
    engines = ", ".join(selection.engines) or "none"
    lines = [
        "## GCI engine selection",
        "",
        f"**Mode:** {selection.mode} | **Engines:** {engines}",
        "",
        "| Path | Effect |",
        "| --- | --- |",
    ]
    lines += [f"| `{path}` | {effect} |" for path, effect in selection.reasons[:MAX_SUMMARY_ROWS]]
    if len(selection.reasons) > MAX_SUMMARY_ROWS:
        lines.append(f"| ... | {len(selection.reasons) - MAX_SUMMARY_ROWS} more |")
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="")
    parser.add_argument("--force-full", action="store_true")
    parser.add_argument("--render-dump", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.render_dump:
        json.dump(render_dump(args.render_dump), sys.stdout)
        return 0

    selection = resolve(args)
    print(summary(selection))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"mode={selection.mode}\nengines={','.join(selection.engines)}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
            output.write(summary(selection))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
