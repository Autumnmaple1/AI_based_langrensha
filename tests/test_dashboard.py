"""Run dependency-free browser view-model regression tests when Node is available."""
import shutil
import subprocess
from pathlib import Path
import pytest


@pytest.mark.skipif(shutil.which("node") is None,reason="Node is optional; run node --test tests/dashboard.test.mjs for frontend checks")
def test_dashboard_view_model():
    result=subprocess.run(["node","--test","tests/dashboard.test.mjs"],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,encoding="utf-8")
    assert result.returncode==0,result.stdout+result.stderr
