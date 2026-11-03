"""Every script the screens load parses: one slip (a name declared twice) stops a whole page's script, and nothing
else would notice until someone clicked."""
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = sorted((Path(__file__).resolve().parent.parent / "app" / "static").glob("*.js"))


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to read the scripts")
@pytest.mark.parametrize("script", SCRIPTS, ids=[s.name for s in SCRIPTS])
def test_each_script_parses(script):
    done = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
