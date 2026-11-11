#!/usr/bin/env python3
"""Run the staged or bundled jbig2, jbig2dec, and jpegtran."""
import argparse, os, subprocess, sys


def _run(path, args):
    env = os.environ.copy()
    env["PATH"] = os.path.dirname(path)
    env.pop("LD_LIBRARY_PATH", None)
    env.pop("DYLD_LIBRARY_PATH", None)
    env.pop("DYLD_FALLBACK_LIBRARY_PATH", None)
    proc = subprocess.run([path] + args, env=env, capture_output=True, text=True)
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    print("%s %s -> %s" % (path, " ".join(args), proc.returncode))
    if text:
        print(text.splitlines()[0])
    if proc.returncode != 0:
        raise SystemExit("command failed: %s %s\n%s" % (path, " ".join(args), text))
    if not text:
        raise SystemExit("no version text from %s" % path)


def _find(directory, names):
    found = []
    for name in names:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate):
            found.append(candidate)
    return found


def smoke_bin(directory):
    tools = {
        "jbig2": (["jbig2", "jbig2.exe"], ["-V"]),
        "jbig2dec": (["jbig2dec", "jbig2dec.exe"], ["--version"]),
        "jpegtran": (["jpegtran", "jpegtran.exe"], ["-version"]),
    }
    for label, (names, args) in tools.items():
        matches = _find(directory, names)
        if not matches:
            raise SystemExit("missing %s in %s" % (label, directory))
        _run(matches[0], args)


def _walk_tools(root):
    wanted = {"jbig2", "jbig2.exe", "jbig2dec", "jbig2dec.exe", "jpegtran", "jpegtran.exe"}
    found = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn in wanted:
                found.append(os.path.join(dirpath, fn))
    return found


def smoke_dist(root):
    found = _walk_tools(root)
    if not found:
        raise SystemExit("no codec programs under %s" % root)
    by_stem = {}
    for path in found:
        stem = os.path.splitext(os.path.basename(path))[0].lower()
        by_stem.setdefault(stem, path)
    args = {"jbig2": ["-V"], "jbig2dec": ["--version"], "jpegtran": ["-version"]}
    missing = [name for name in args if name not in by_stem]
    if missing:
        raise SystemExit("dist is missing %s (found %s)" % (", ".join(missing), found))
    for name, path in by_stem.items():
        _run(path, args[name])


def archive_contains(exe):
    from PyInstaller.archive.readers import CArchiveReader
    reader = CArchiveReader(exe)
    bases = set()
    for name in reader.toc:
        base = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
        bases.add(base)
    for needle in ("jbig2", "jbig2dec", "jpegtran"):
        if needle not in bases and (needle + ".exe") not in bases:
            raise SystemExit("%s is not inside %s" % (needle, exe))
    print("archive %s contains jbig2, jbig2dec, and jpegtran" % exe)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin", help="directory of staged codec programs")
    parser.add_argument("--dist", help="PyInstaller dist directory to search")
    parser.add_argument("--archive", help="onefile executable whose archive must contain the tools")
    args = parser.parse_args()
    if not any((args.bin, args.dist, args.archive)):
        raise SystemExit("pass --bin, --dist, and/or --archive")
    if args.bin:
        smoke_bin(args.bin)
    if args.dist:
        smoke_dist(args.dist)
    if args.archive:
        archive_contains(args.archive)


if __name__ == "__main__":
    main()
