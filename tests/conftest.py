import os, sys, shutil
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA = os.path.join(os.path.dirname(__file__), "data")


@pytest.fixture(scope="session")
def qapp():
    from krop.qt import QApplication
    app = QApplication.instance() or QApplication(["krop-test"])
    app.setOrganizationName("krop-test")
    app.setApplicationName("krop-test")
    yield app


@pytest.fixture
def scan(tmp_path):
    p = tmp_path / "scanlike.pdf"
    shutil.copy(os.path.join(DATA, "scanlike.pdf"), p)
    return str(p)


def new_window(log):
    from krop.activitylog import install, patch_classes
    patch_classes(log)
    from krop.mainwindow import MainWindow
    w = MainWindow()
    install(w, log)
    w.logPath = log
    return w


@pytest.fixture
def window(qapp, tmp_path):
    """A krop MainWindow with the activity log going to tmp_path/activity.log."""
    w = new_window(str(tmp_path / "activity.log"))
    yield w
    w.close()
    qapp.processEvents()
