"""Tests for LOC calibration against git ground truth."""
from __future__ import annotations

import subprocess
from pathlib import Path

from tcer.core import calibrate, loc


def test_parse_numstat_line():
    """Test parsing of git log --numstat lines."""
    # Normal line
    result = calibrate._parse_numstat_line("10\t5\tfile.py")
    assert result == (10, 5, "file.py")

    # Binary file (should be filtered)
    result = calibrate._parse_numstat_line("-\t-\timage.png")
    assert result is None

    # Non-code file (should be filtered)
    result = calibrate._parse_numstat_line("10\t5\tREADME.txt")
    assert result is None

    # Quoted path (git quotes paths with special chars)
    result = calibrate._parse_numstat_line('10\t5\t"path with spaces.py"')
    assert result == (10, 5, "path with spaces.py")

    # Invalid format
    result = calibrate._parse_numstat_line("invalid line")
    assert result is None


def test_git_commits_in_window_no_git(tmp_path: Path):
    """Test git_commits_in_window when no .git directory exists."""
    commits = calibrate.git_commits_in_window(tmp_path, None, None)
    assert commits == []


def test_session_calibration_properties():
    """Test SessionCalibration deviation calculations."""
    cal = calibrate.SessionCalibration(
        session_id="test",
        tcer_added=100,
        tcer_deleted=10,
        git_added=80,
        git_deleted=5,
    )

    assert cal.added_deviation == 20  # tcer over-counted added
    assert cal.deleted_deviation == 5  # tcer under-counted deleted
    assert cal.net_deviation == 15  # net overestimation


def test_calibrate_project_no_project(tmp_path: Path):
    """Test calibrate_project with non-existent project."""
    result = calibrate.calibrate_project("nonexistent-project-xyz")
    assert result == []


def test_integration_calibrate_real_project():
    """Integration test: calibrate TCER project if in repo."""
    # This test only runs if we're actually in the TCER git repo
    try:
        subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True,
            check=True,
            timeout=5,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        # Not in a git repo or git not available, skip
        return

    # Try to calibrate - should not crash
    results = calibrate.calibrate_project("TCER", code_dir=Path.cwd())

    # If results exist, validate structure
    if results:
        for cal in results:
            assert isinstance(cal.session_id, str)
            assert cal.tcer_added >= 0
            assert cal.tcer_deleted >= 0
            assert cal.git_added >= 0
            assert cal.git_deleted >= 0
