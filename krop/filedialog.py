# -*- coding: utf-8 -*-
"""Choose the native or Qt file dialog for Open."""


def use_native_file_dialog(platform_name, sys_platform, force_qt=False):
    """Return True when Open should use the platform's native file dialog.

    Native is used only on macOS when Qt is running its Cocoa plugin
    (not, for example, under QT_QPA_PLATFORM=xcb). force_qt, set by
    --qt-file-dialog or KROPPY_QT_FILE_DIALOG, keeps the Qt dialog.
    """
    if force_qt:
        return False
    return sys_platform == "darwin" and platform_name == "cocoa"


def qt_file_dialog_requested(env=None):
    """Return True if KROPPY_QT_FILE_DIALOG requests the Qt file dialog."""
    if env is None:
        import os
        env = os.environ
    val = str(env.get("KROPPY_QT_FILE_DIALOG", "")).strip().lower()
    return val in ("1", "true", "yes", "on")
