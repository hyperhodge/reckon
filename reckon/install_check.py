# SPDX-License-Identifier: Apache-2.0
"""Decision 1, RECKON-1.1-SPEC.md: reckon runs only from a downloaded copy,
installed with `pip install -e .`.

Every module beside this one keeps its data next to its own code -- HERE and
ROOT paths, sys.path inserts -- rather than reading from a configured data
folder (see cli.py). That is only safe if the code actually running IS the
downloaded copy: a non-editable install copies files into site-packages and
leaves them to drift from the folder a person read and approved. One check
here stands in for a data-folder setting touching every module.
"""

import json
from importlib import metadata

INSTALL_LINE = "pip install -e ."


class NotLinkedInstall(Exception):
    """reckon is not running from an editable install of a downloaded copy."""


def _distribution(name="reckon"):
    return metadata.distribution(name)


def require_linked_install(distribution_fn=_distribution):
    """Raise NotLinkedInstall unless reckon is installed editable (PEP 660).

    distribution_fn is injectable so a test can simulate every install state
    without actually installing anything.
    """
    try:
        dist = distribution_fn()
    except metadata.PackageNotFoundError:
        raise NotLinkedInstall(
            f"reckon is not installed as a package -- download it and run: {INSTALL_LINE}")

    try:
        raw = dist.read_text("direct_url.json")
    except Exception:
        raw = None
    if not raw:
        raise NotLinkedInstall(
            f"reckon was installed without the metadata that proves it is a downloaded "
            f"copy -- reinstall from your copy with: {INSTALL_LINE}")

    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        info = {}
    editable = bool((info.get("dir_info") or {}).get("editable"))
    if not editable:
        raise NotLinkedInstall(
            f"reckon must be installed from a downloaded copy with `{INSTALL_LINE}`, not a "
            f"regular install -- reinstall from your copy with: {INSTALL_LINE}")
    return dist
