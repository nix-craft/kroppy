"""The program introduces itself as kroppy and keeps old settings."""
import os, subprocess, sys

from krop.version import __version__


def test_version_and_help_say_kroppy():
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
               PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ver = subprocess.run([sys.executable, "-m", "krop", "--version"],
                         env=env, capture_output=True, text=True, check=True)
    assert ver.stdout.strip() == "kroppy " + __version__
    help_r = subprocess.run([sys.executable, "-m", "krop", "--help"],
                            env=env, capture_output=True, text=True, check=True)
    assert help_r.stdout.startswith("usage: kroppy ")
    assert ".kroppy.log" in help_r.stdout
    assert "krop-trimall" not in help_r.stdout.split("usage:", 1)[0]


def test_window_and_about_say_kroppy(qapp, tmp_path):
    from krop.qt import QApplication
    from krop.mainwindow import MainWindow
    app = QApplication.instance()
    saved = (app.organizationName(), app.organizationDomain(),
             app.applicationName(), app.applicationDisplayName())
    app.setOrganizationName("kroppy-brand-test")
    app.setOrganizationDomain("kroppy-brand-test")
    app.setApplicationName("kroppy")
    app.setApplicationDisplayName("kroppy")
    try:
        w = MainWindow()
        assert w.windowTitle() == "kroppy"
        help_menu = w.menuBar().actions()
        assert any("Help" in a.text() for a in help_menu)
        about = help_menu[0].menu().actions()[0]
        assert about.text() == "About kroppy"
        from krop.qt import QTimer
        seen = []

        def close_box():
            box = QApplication.activeModalWidget()
            seen.append("" if box is None else box.text())
            if box is not None:
                box.close()

        QTimer.singleShot(0, close_box)
        w.slotAbout()
        assert seen and "kroppy" in seen[0] and __version__ in seen[0]
        w.close()
    finally:
        app.setOrganizationName(saved[0])
        app.setOrganizationDomain(saved[1])
        app.setApplicationName(saved[2])
        app.setApplicationDisplayName(saved[3] or "")


def test_qsettings_migrated_once(qapp):
    from krop.qt import QApplication, QSettings
    from krop.application import migrate_qsettings
    app = QApplication.instance()
    saved = (app.organizationName(), app.organizationDomain(),
             app.applicationName(), app.applicationDisplayName())
    org = "kroppy-migrate-test"
    app.setOrganizationName(org)
    app.setOrganizationDomain(org)
    try:
        app.setApplicationName("krop")
        old = QSettings()
        old.clear()
        old.setValue("PDF/HardCrop", "true")
        old.setValue("Trim/Dust", "9")
        old.sync()
        app.setApplicationName("kroppy")
        app.setApplicationDisplayName("kroppy")
        fresh = QSettings()
        fresh.clear()
        fresh.sync()
        migrate_qsettings()
        assert QSettings().value("PDF/HardCrop") == "true"
        assert QSettings().value("Trim/Dust") == "9"
        QSettings().setValue("PDF/HardCrop", "false")
        QSettings().sync()
        migrate_qsettings()
        assert QSettings().value("PDF/HardCrop") == "false"
        assert QSettings().value("Trim/Dust") == "9"
    finally:
        app.setApplicationName("krop")
        QSettings().clear()
        QSettings().sync()
        app.setApplicationName("kroppy")
        QSettings().clear()
        QSettings().sync()
        app.setOrganizationName(saved[0])
        app.setOrganizationDomain(saved[1])
        app.setApplicationName(saved[2])
        app.setApplicationDisplayName(saved[3] or "")


def test_setup_keeps_command_aliases():
    text = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "setup.py")).read()
    assert "'kroppy = krop.__main__:main'" in text
    assert "'krop = krop.__main__:main'" in text
    assert "'krop-trimall = krop.__main__:main'" in text
    yml = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            ".github", "workflows", "build.yml")).read()
    assert "krop-trimall-" not in yml
    assert "kroppy-${{ steps.ver.outputs.v }}-x86_64.AppImage" in yml
    assert "kroppy-${{ matrix.name }}" in yml
    assert "com.arminstraub.krop" in yml
    assert "--name kroppy" in yml
