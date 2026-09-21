"""
What `boxlite.cloud` costs to import, and what it is called.

Both are cross-release promises. The import path is what user code is written
against, and it has to survive the internals being replaced by a generated
client. The dependency count is what makes `pip install boxlite` safe to put
in front of an agent runtime: the package declares none, and a catalog client
is not worth the first one.
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_the_import_path_is_boxlite_cloud():
    """C12: the names user code is written against, at the path it uses."""
    from boxlite.cloud import (  # noqa: F401
        CloudClient,
        ImageLimitExceeded,
        ImageNotFound,
        SyncCloudClient,
    )


def test_the_package_declares_no_runtime_dependencies():
    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover - the floor of the supported range
        import tomli as tomllib

    manifest = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text())

    # Optional groups may declare whatever they need; the base install is what
    # every user pays for, and it stays empty.
    assert manifest["project"]["dependencies"] == []


def test_the_cloud_client_imports_on_a_bare_install():
    """
    The declaration above is a promise; this is the observation.

    Run with site-packages taken off the path, so the interpreter has the
    standard library and this package and nothing else — which is what
    `pip install boxlite` leaves behind. A developer checkout has the optional
    extras installed (greenlet, cloudpickle) and would hide a new dependency
    behind them, so the check has to remove them rather than look for them.
    """
    probe = textwrap.dedent(
        """
        import json, sys, sysconfig, warnings

        roots = tuple(
            path
            for key in ("purelib", "platlib")
            for path in (sysconfig.get_paths().get(key),)
            if path
        )
        sys.path = [entry for entry in sys.path if not str(entry).startswith(roots)]

        warnings.simplefilter("ignore")
        from boxlite.cloud import CloudClient, ImageNotFound, SyncCloudClient  # noqa: F401

        outside = sorted(
            name
            for name, module in sys.modules.items()
            if getattr(module, "__file__", None) and str(module.__file__).startswith(roots)
        )
        print(json.dumps(outside))
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=PACKAGE_ROOT,
        capture_output=True,
        text=True,
        # Checked below, so the failure names the missing module rather than
        # arriving as a bare non-zero exit.
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [], result.stdout
