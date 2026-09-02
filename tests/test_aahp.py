import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_validate_manifest() -> None:
    result = subprocess.run(
        ["python3", "tools/aahp.py", "validate-manifest"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["expanded_file_count"] > 0


def test_generate_checksums() -> None:
    result = subprocess.run(
        ["python3", "tools/aahp.py", "generate-checksums"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    checksum_file = ROOT / payload["output"]
    checksum_data = json.loads(checksum_file.read_text())
    assert payload["ok"] is True
    assert checksum_file.exists()
    assert "Project_spec2.md" in checksum_data["files"]
    assert all(
        "__pycache__" not in filename and not filename.endswith((".pyc", ".pyo"))
        for filename in checksum_data["files"]
    )
