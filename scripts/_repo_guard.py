"""Fail closed before SkyBuild automation touches Git or GitHub state."""

import re
import subprocess
from pathlib import Path


class RepoGuardError(Exception):
    pass


_SKYBUILD_ORIGIN = re.compile(
    r"(?:https://(?:[^/@]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"stonesky-ai/skybuild(?:\.git)?/?",
    re.IGNORECASE,
)


def verify_skybuild(checkout: Path) -> Path:
    """Return exact SkyBuild Git root; never accept SkyKeep or a parent path."""
    checkout = checkout.resolve()
    try:
        root = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=checkout,
                              text=True, capture_output=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise RepoGuardError("checkout must be a SkyBuild Git root with origin") from error
    if Path(root).resolve() != checkout:
        raise RepoGuardError("--checkout must name the exact SkyBuild Git root")
    if not (checkout / "src/skybuild/store.py").is_file() or not (checkout / "docs/design/architecture.md").is_file():
        raise RepoGuardError("SkyBuild source markers are missing")
    verify_skybuild_remote(checkout, "origin")
    return checkout


def verify_skybuild_remote(checkout: Path, remote: str) -> None:
    """Require every fetch and push URL of a selected remote to name SkyBuild."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", remote):
        raise RepoGuardError("invalid remote name")
    try:
        fetch_urls = subprocess.run(["git", "remote", "get-url", "--all", remote], cwd=checkout,
                                    text=True, capture_output=True, check=True).stdout.splitlines()
        push_urls = subprocess.run(["git", "remote", "get-url", "--push", "--all", remote],
                                   cwd=checkout, text=True, capture_output=True, check=True).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError) as error:
        raise RepoGuardError("selected remote is unavailable") from error
    if not fetch_urls or not push_urls or any(not _SKYBUILD_ORIGIN.fullmatch(url)
                                               for url in fetch_urls + push_urls):
        raise RepoGuardError("selected remote fetch/push URLs are not all stonesky-ai/skybuild")
