"""Check coding prerequisites without fetching, installing, or executing work."""

import argparse
import fnmatch
import json
import os
from pathlib import Path
import subprocess
import sys


class PreflightError(Exception):
    """A prerequisite needs correction before dependent work."""


def _run(checkout, arguments, *, env=None):
    try:
        result = subprocess.run(arguments, cwd=checkout, env=env, capture_output=True,
                                text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise PreflightError("command_unavailable_or_timeout") from None
    if result.returncode:
        raise PreflightError("command_failed")
    return result.stdout.strip()


def _origin(value):
    return value.removesuffix(".git").rstrip("/") in {
        "https://github.com/stonesky-ai/skybuild", "git@github.com:stonesky-ai/skybuild",
        "ssh://git@github.com/stonesky-ai/skybuild",
    }


def preflight(checkout, *, required=(), discover=(), refs=(), interpreter=None, cache=None):
    """Fail at the first prerequisite; never invoke the requested dependent task."""
    checkout = Path(checkout).resolve()
    if not checkout.is_dir():
        raise PreflightError("checkout_missing")
    root = _run(checkout, ["git", "rev-parse", "--show-toplevel"])
    if Path(root).resolve() != checkout:
        raise PreflightError("checkout_root_mismatch")
    for option in ([], ["--push"]):
        urls = _run(checkout, ["git", "remote", "get-url", *option, "--all", "origin"]).splitlines()
        if not urls or not all(_origin(url) for url in urls):
            raise PreflightError("origin_mismatch")

    checked = []
    for value in required:
        path = Path(value)
        if not path.is_absolute():
            path = checkout / path
        if not path.is_file():
            raise PreflightError("required_file_missing: " + str(value))
        checked.append(str(value))

    resolved_refs = {}
    for ref in refs:
        # Require explicit ref names, not arbitrary revision expressions/options.
        if not ref.startswith("refs/") or any(c in ref for c in "\n\r\0"):
            raise PreflightError("explicit_ref_required")
        try:
            resolved_refs[ref] = _run(checkout, ["git", "rev-parse", "--verify", ref + "^{commit}"])
        except PreflightError:
            raise PreflightError("ref_unavailable_fetch_explicitly: " + ref) from None

    discovered = {}
    if discover:
        files = _run(checkout, ["git", "ls-files", "-z"]).split("\0")
        for pattern in discover:
            matches = sorted(path for path in files if path and fnmatch.fnmatchcase(path, pattern))
            if not matches:
                raise PreflightError("discovery_no_match: " + pattern)
            discovered[pattern] = {"count": len(matches), "paths": matches[:20]}

    cache_result = None
    if cache is not None:
        cache_path = Path(cache).resolve()
        # Validate an existing ancestor without creating directories or probe files.
        ancestor = cache_path
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        if not ancestor.is_dir() or not os.access(ancestor, os.W_OK | os.X_OK):
            raise PreflightError("cache_not_writable")
        cache_result = str(cache_path)

    imported = None
    if interpreter is not None:
        interpreter = Path(interpreter).absolute()
        if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
            raise PreflightError("interpreter_unavailable")
        # Match project_python's source boundary; never sync/install dependencies.
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(str(checkout / part) for part in ("src", "scripts", "."))
        env.pop("UV_WORKING_DIR", None)
        try:
            imported = _run(checkout, [str(interpreter), "-P", "-c",
                                      "import skybuild; print(skybuild.__file__)"], env=env)
        except PreflightError:
            raise PreflightError("source_import_failed") from None
        if Path(imported).resolve() != (checkout / "src/skybuild/__init__.py").resolve():
            raise PreflightError("source_import_mismatch")

    return {"ok": True, "checkout": str(checkout), "origin": "stonesky-ai/skybuild",
            "required": checked, "refs": resolved_refs, "discovered": discovered,
            "cache": cache_result, "imported_source": imported}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--require", action="append", default=[], help="Existing file, relative to checkout or explicit absolute evidence path")
    parser.add_argument("--discover", action="append", default=[], help="Tracked relative filename glob; outputs at most 20 matches")
    parser.add_argument("--ref", action="append", default=[], help="Existing explicit refs/... name; does not fetch")
    parser.add_argument("--python", type=Path, help="Existing interpreter; verifies this checkout's source without uv sync")
    parser.add_argument("--uv-cache", type=Path, help="Validate cache location or nearest existing parent without creating it")
    args = parser.parse_args(argv)
    try:
        result = preflight(args.checkout, required=args.require, discover=args.discover,
                           refs=args.ref, interpreter=args.python, cache=args.uv_cache)
    except PreflightError as error:
        print(json.dumps({"ok": False, "reason": str(error)}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
