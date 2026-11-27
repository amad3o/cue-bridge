"""The server must start with only the core dependencies, as in CI. A developer's machine usually has the extras too."""

import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).parents[2] / "src"

# Packages that come only with the optional extras. A None entry in sys.modules makes importing one fail.
BLOCK = "import sys; sys.modules.update(dict.fromkeys(['numpy', 'librosa', 'soundfile', 'pyrekordbox', 'sqlcipher3']))"


def run_without_extras(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", f"{BLOCK}\n{code}"], env={"PYTHONPATH": str(SRC)}, capture_output=True, text=True, timeout=60)


def test_the_server_imports_without_the_optional_extras():
    result = run_without_extras("import cuebridge.mcp_server")

    assert result.returncode == 0, result.stderr[-1500:]


def test_analysing_without_the_extra_says_how_to_install_it():
    result = run_without_extras(
        "from cuebridge.services.beatgrid import analyzer\n"
        "try:\n    analyzer.analyze_file('x.wav')\n"
        "except analyzer.AnalysisError as error:\n    print(error)"
    )

    assert result.returncode == 0, result.stderr[-1500:]
    assert 'pip install -e ".[beatgrid]"' in result.stdout
