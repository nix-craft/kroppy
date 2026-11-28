"""Native vs Qt file-dialog selection, without a GUI."""
import os, subprocess, sys
from krop.filedialog import qt_file_dialog_requested, use_native_file_dialog


def test_native_only_on_mac_cocoa():
    assert use_native_file_dialog("cocoa", "darwin", force_qt=False) is True
    assert use_native_file_dialog("cocoa", "darwin", force_qt=True) is False
    assert use_native_file_dialog("xcb", "darwin", force_qt=False) is False
    assert use_native_file_dialog("cocoa", "linux", force_qt=False) is False
    assert use_native_file_dialog("xcb", "linux", force_qt=False) is False
    assert use_native_file_dialog("windows", "win32", force_qt=False) is False
    assert use_native_file_dialog("", "darwin", force_qt=False) is False


def test_qt_file_dialog_env():
    assert qt_file_dialog_requested({}) is False
    assert qt_file_dialog_requested({"KROPPY_QT_FILE_DIALOG": "1"}) is True
    assert qt_file_dialog_requested({"KROPPY_QT_FILE_DIALOG": "true"}) is True
    assert qt_file_dialog_requested({"KROPPY_QT_FILE_DIALOG": "YES"}) is True
    assert qt_file_dialog_requested({"KROPPY_QT_FILE_DIALOG": "on"}) is True
    assert qt_file_dialog_requested({"KROPPY_QT_FILE_DIALOG": "0"}) is False
    assert qt_file_dialog_requested({"KROPPY_QT_FILE_DIALOG": "false"}) is False
    assert qt_file_dialog_requested({"KROPPY_QT_FILE_DIALOG": ""}) is False


def test_help_lists_qt_file_dialog():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=root)
    result = subprocess.run(
        [sys.executable, "-m", "krop", "--help"],
        env=env, capture_output=True, text=True, check=True)
    assert "--qt-file-dialog" in result.stdout
    assert "KROPPY_QT_FILE_DIALOG" in result.stdout
