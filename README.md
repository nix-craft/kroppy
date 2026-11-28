# kroppy [![License: GPL-3.0](https://img.shields.io/github/license/arminstraub/krop.svg)](https://www.gnu.org/licenses/gpl-3.0) ![Latest release](https://img.shields.io/github/v/tag/arminstraub/krop.svg)

kroppy is a simple graphical tool to crop the pages of PDF files.

It is written in Python and relies on PyQt and PyMuPDF (or a suitable subset of pypdf/pikepdf/python-poppler-qt) for its functionality. A unique feature of kroppy is its ability to automatically split pages into subpages to fit the limited screen size of devices such as eReaders. This is particularly useful, if your eReader does not support convenient scrolling.

The command is `kroppy`. `krop` and `krop-trimall` run the same program.

A running window can take local commands on a Unix socket. It is off unless you pass `--control [PATH]` or set `KROPPY_CONTROL`. See [docs/control.md](docs/control.md). `python -m krop.ctl` (or `kroppyctl`) sends one command.

kroppy is built from [krop](https://arminstraub.com/software/krop) by Armin Straub.

![Screenshot](https://arminstraub.com/images/krop/screenshot.png)
