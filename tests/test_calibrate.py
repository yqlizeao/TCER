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


def test_format_calibration_report_empty():
    """Test report formatting with no calibrations."""
    report = calibrate.format_calibration_report([])
    assert "No sessions to calibrate" in report


def test_format_calibration_report():
    """Test report formatting with sample data."""
    calibrations = [
        calibrate.SessionCalibration("session1", 100, 10, 100, 10),
        calibrate.SessionCalibration("session2", 200, 20, 180, 15),
        calibrate.SessionCalibration("session3", 50, 5, 60, 8),
    ]

    report = calibrate.format_calibration_report(calibrations)

    # Check structure
    assert "TCER LOC Calibration Report" in report
    assert "session1" in report
    assert "session2" in report
    assert "session3" in report
    assert "Summary" in report
    assert "Calibration Factor" in report
    assert "Total TCER net LOC" in report
    assert "Total Git net LOC" in report

    # Check calculations
    total_tcer_net = (100 - 10) + (200 - 20) + (50 - 5)  # 90 + 180 + 45 = 315
    total_git_net = (100 - 10) + (180 - 15) + (60 - 8)  # 90 + 165 + 52 = 307
    assert "+315" in report or "315" in report
    assert "+307" in report or "307" in report


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

        # Format report should work
        report = calibrate.format_calibration_report(results)
        assert len(report) > 100  # Should be substantial
        assert "TCER LOC Calibration Report" in report
