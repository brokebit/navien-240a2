"""The package version: written into the .deb by scripts/build-deb.sh (navien/_version.py),
otherwise derived from `git describe` in a dev tree, the same way build-deb.sh does."""

import re
import subprocess
from pathlib import Path


def _from_git() -> str:
    try:
        desc = subprocess.run(
            ["git", "describe", "--tags", "--match", "v[0-9]*", "--dirty"],
            cwd=Path(__file__).parent, capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    # v0.7.2 -> 0.7.2 ; v0.7.2-3-gabc1234 -> 0.7.2+git3.gabc1234 ; ...-dirty -> ...+dirty
    v = re.sub(r"^v", "", desc)
    v = re.sub(r"-([0-9]+)-(g[0-9a-f]+)", r"+git\1.\2", v)
    v = re.sub(r"-dirty$", "+dirty", v)
    return re.sub(r"\+git(.*)\+dirty$", r"+git\1.dirty", v)


try:
    from ._version import VERSION
except ImportError:
    VERSION = _from_git()
