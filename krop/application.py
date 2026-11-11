# -*- coding: iso-8859-1 -*-

"""
kroppy: A tool to crop PDF files

You can use command line arguments in addition to (or, to a degree, instead of) the graphical interface.

For instance, to automatically undo 4 pages print onto a single page:
    kroppy --go --grid=2x2 file.pdf
To additionally trim each of these pages:
    kroppy --go --grid=2x2 --trim --trim-use=all file.pdf

Automatically crop the margins every single page individually:
    kroppy --go --selections=individual --grid=1@all --trim file.pdf
Omit the --go to further edit the selections in the graphical interface before cropping.

Copyright (C) 2010-2025 Armin Straub, http://arminstraub.com
"""

"""
This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 3 of the License, or
(at your option) any later version.
"""

import sys

from krop.version import __version__


def _run_analyze_cli(args):
    import json
    from krop.analyze import analyze_file, format_report
    data = analyze_file(args.file)
    if args.analyze_only:
        print(format_report(data))
    if args.analyze_json:
        with open(args.analyze_json, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
    _append_analyze_log(args, data)


def _append_analyze_log(args, data):
    """Write ANALYZE lines the same way a run log would, then drop a clean one."""
    import os
    import time
    from krop.analyze import analyze_lines
    path = args.log or os.environ.get("KROP_LOG")
    keep = getattr(args, "log_keep", None) or "on-error"
    if path and str(path).lower() in ("off", "0", "no", "false"):
        return
    if keep == "never" and not (path and str(path).lower() not in ("off", "0", "no", "false")):
        return
    explicit = bool(path) and str(path).lower() not in ("off", "0", "no", "false")
    if explicit:
        log_path = os.path.expanduser(path)
    else:
        loc = getattr(args, "log_location", None) or "beside"
        stem = os.path.splitext(os.path.basename(args.file))[0] + ".kroppy.log"
        if loc == "central":
            from krop.activitylog import user_log_dir
            log_path = os.path.join(user_log_dir(), stem)
        else:
            log_path = os.path.splitext(os.path.abspath(args.file))[0] + ".kroppy.log"
    parent = os.path.dirname(log_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(log_path, "a", buffering=1) as fh:
        for line in analyze_lines(data):
            stamp = time.strftime("%H:%M:%S") + ".%03d" % (int(time.time() * 1000) % 1000)
            fh.write("%s ANALYZE %s\n" % (stamp, line))
            fh.flush()
    if explicit or keep == "always":
        return
    if keep in ("on-error", None):
        os.remove(log_path)


def migrate_qsettings():
    """Copy settings saved when the application name was "krop", once.

    The organization stays arminstraub.com. Only the application name changes,
    which is what selects the QSettings file (or registry key, or plist).
    """
    from krop.qt import QApplication, QSettings
    app = QApplication.instance()
    if app is None:
        return
    new_name = app.applicationName()
    if new_name == "krop":
        return
    app.setApplicationName(new_name)
    if QSettings().value("MigratedFrom/krop") == "true":
        return
    app.setApplicationName("krop")
    old = QSettings()
    old.sync()
    saved = [(key, old.value(key)) for key in old.allKeys()]
    app.setApplicationName(new_name)
    new = QSettings()
    for key, value in saved:
        if not new.contains(key):
            new.setValue(key, value)
    new.setValue("MigratedFrom/krop", "true")
    new.sync()


def main():
    from argparse import ArgumentParser, RawTextHelpFormatter
    parser = ArgumentParser(prog='kroppy', description=__doc__,
            formatter_class=RawTextHelpFormatter)

    parser.add_argument('-v', '--version', action='version', version='%(prog)s ' + __version__)

    parser.add_argument('file', nargs='?', help='PDF file to open')
    parser.add_argument('-o', '--output', help='where to save the cropped PDF')
    parser.add_argument('--whichpages', help='which pages (e.g. "1-5" or "1,3-") to include in cropped PDF (default: all)')
    parser.add_argument('--rotate', type=int, choices=[0,90,180,270], help='how much to rotate the cropped pdf clockwise (default: 0)')
    parser.add_argument('--optimize', choices=['gs', 'no'], help='whether to optimize the final PDF using ghostscript (default: previous choice)')
    parser.add_argument('--hard-crop', action='store_true', help='hard-crop embedded images to the visible page box when saving, keeping their original encoding (default: previous choice, initially off)')

    parser.add_argument('--grid', help='if set to 2x3, for instance, creates a 2x3 grid of selections on initial page; if only one number is specified, the number of columns/rows is determined according to whether the page is landscape or portrait')

    parser.add_argument('--initialpage', help='which page to open initially (default: 1)')
    parser.add_argument('--selections', type=str, choices=['all', 'evenodd', 'individual'], help='to which pages should selections apply')
    parser.add_argument('--exceptions', help='pages (e.g. "1-5" or "1,3-") which require individual selections')

    parser.add_argument('--trim', action='store_true', help='if specified, will auto trim initial selections')
    parser.add_argument('--trim-use', type=str, choices=['initial', 'all'], help='whether to inspect only the initial page or all pages (slow!) when auto trimming (default: previous value)')
    parser.add_argument('--trim-padding', help='how much padding to include when auto trimming (default: previous value)')

    parser.add_argument('--log', help='run log to append to, one flushed line per event. A path is always kept. "off" disables logging. By default the log is <input>.kroppy.log beside the PDF and is deleted when the session ends with no errors. $KROP_LOG is used when this option is omitted')
    parser.add_argument('--log-keep', choices=['always', 'on-error', 'never'], help='always: keep the run log; on-error: delete it when the session ends with no errors (default); never: do not write a run log (same as --log off). Ignored when --log names a file, which is always kept')
    parser.add_argument('--log-location', choices=['beside', 'central'], help='where an automatic run log is written: beside the PDF (default) or a central folder (~/.local/state/kroppy, ~/Library/Logs/kroppy, or %%LOCALAPPDATA%%\\kroppy\\logs). Ignored when --log names a file. Central logs older than 30 days are removed on startup')
    parser.add_argument('--go', action='store_true', help='output PDF without opening the kroppy window (using the choices supplied on the command line); if used in a script without X server access, you can run kroppy using xvfb-run')
    parser.add_argument('--analyze-only', action='store_true', help='print a color/bit-depth recommendation and exit without opening the window')
    parser.add_argument('--analyze-json', metavar='FILE', help='write the full analysis JSON to FILE and exit without opening the window')

    parser.add_argument('--no-recover-crops', action='store_true', help='open an already-cropped PDF as it is, instead of at full page size with its crop boxes as editable selections')
    parser.add_argument('--use-qt5', action='store_true', help='use PyQt5 instead of PyQt6 (default: use PyQt6 if available)')
    parser.add_argument('--use-pymupdf', action='store_true', help='use PyMuPDF for rendering and cropping (default)')
    parser.add_argument('--use-pikepdf', action='store_true', help='use pikepdf for cropping (PyQt5 only, default: use PyMuPDF)')
    parser.add_argument('--use-pypdf', action='store_true', help='use pypdf for cropping (PyQt5 only, default: use PyMuPDF)')
    parser.add_argument('--use-pypdf2', action='store_true', help='use PyPDF2 for cropping (PyQt5 only, default: use PyMuPDF)')
    parser.add_argument('--use-poppler', action='store_true', help='use Poppler Qt for rendering (PyQt5 only, default: use PyMuPDF)')

    args = parser.parse_args()

    if args.analyze_only or args.analyze_json:
        if not args.file:
            parser.error("a PDF file is required")
        _run_analyze_cli(args)
        return

    from krop.qt import QApplication
    app = QApplication(sys.argv)
    app.setOrganizationName("arminstraub.com")
    app.setOrganizationDomain("arminstraub.com")
    app.setApplicationName("kroppy")
    app.setApplicationDisplayName("kroppy")
    migrate_qsettings()

    import os
    from krop.activitylog import install as _installLog, patch_classes
    _logPath = args.log or os.environ.get('KROP_LOG')
    _explicit_log = _logPath and str(_logPath).lower() not in ("off", "0", "no", "false")
    patch_classes("off" if args.log_keep == "never" and not _explicit_log else _logPath)
    from krop.mainwindow import MainWindow
    window=MainWindow()
    _installLog(window, _logPath, keep=args.log_keep, location=args.log_location)

    window.analyzeOnOpen = not args.go
    if args.file is not None:
        fileName = args.file
        window.openFile(fileName, False if args.no_recover_crops else None)

    if args.output is not None:
        window.ui.editFile.setText(args.output)
    if args.whichpages is not None:
        window.ui.editWhichPages.setText(args.whichpages)
    if args.rotate is not None:
        window.ui.comboRotation.setCurrentIndex({0:0,90:2,180:3,270:1}[args.rotate])
    if args.optimize is not None:
        window.ui.checkGhostscript.setChecked(args.optimize == "gs")
    if args.hard_crop:
        window.ui.checkHardCrop.setChecked(True)
    if args.selections is not None:
        if args.selections == "all":
            window.ui.radioSelAll.setChecked(True)
        elif args.selections == "evenodd":
            window.ui.radioSelEvenOdd.setChecked(True)
        elif args.selections == "individual":
            window.ui.radioSelIndividual.setChecked(True)
    if args.exceptions is not None:
        window.ui.editSelExceptions.setText(args.exceptions)
        window.slotSelExceptionsChanged()
    if args.initialpage is not None:
        window.ui.editCurrentPage.setText(args.initialpage)
        window.slotCurrentPageEdited(args.initialpage)
    if args.trim_use is not None:
        window.ui.checkTrimUseAllPages.setChecked(args.trim_use == "all")
    if args.trim_padding is not None:
        window.ui.editPadding.setText(args.trim_padding)

    # args.grid is specified as 2x3 for 2 cols, 3 rows
    if args.grid:
        window.createSelectionGrid(args.grid)

    if args.trim:
        window.slotTrimMarginsAll()

    # shut down on ctrl+c when pressed in terminal (not gracefully, though)
    # http://stackoverflow.com/questions/4938723/
    import signal
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    if args.go:
        #  sys.stdout.write('kropping...\n')
        from krop.qt import QTimer
        QTimer.singleShot(0, window.slotKrop)
        QTimer.singleShot(0, window.close)
        # the window was never shown, so closing it doesn't end the event loop
        QTimer.singleShot(0, app.quit)
    else:
        window.show()
        window.slotFitInView(window.ui.actionFitInView.isChecked())

    sys.exit(app.exec())
