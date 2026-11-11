#!/usr/bin/env python3
"""Stage jbig2, jbig2dec, and jpegtran for the PyInstaller bundle.

Run on the build machine (Linux, macOS, or Windows) after those programs are
installed. Copies each program, the shared libraries it needs beyond the
system C library, and license notices into DEST/bin and DEST/licenses.

    python packaging/stage_codecs.py --dest packaging/codecs
"""
import argparse, os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
LICENSE_SRC = os.path.join(HERE, "codec-licenses")
TOOLS = ("jbig2", "jbig2dec", "jpegtran")

# Host C library and loader. Everything else the tools need is copied.
LINUX_SKIP_PREFIXES = ("ld-linux", "linux-vdso")
LINUX_SKIP_SONAMES = {
    "libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0", "librt.so.1",
    "libresolv.so.2", "libutil.so.1", "libstdc++.so.6", "libgcc_s.so.1",
}
WINDOWS_SKIP = {
    "kernel32.dll", "kernelbase.dll", "user32.dll", "gdi32.dll", "advapi32.dll",
    "shell32.dll", "ole32.dll", "oleaut32.dll", "comdlg32.dll", "ws2_32.dll",
    "ntdll.dll", "msvcrt.dll", "ucrtbase.dll", "sechost.dll", "rpcrt4.dll",
    "imm32.dll", "setupapi.dll", "version.dll", "userenv.dll", "bcrypt.dll",
    "crypt32.dll", "winmm.dll", "shlwapi.dll", "comctl32.dll", "psapi.dll",
    "iphlpapi.dll", "dbghelp.dll", "normaliz.dll", "cfgmgr32.dll", "powrprof.dll",
}


def _run(cmd):
    return subprocess.check_output(cmd, text=True, errors="replace")


def _which_all(name):
    found = []
    path = os.environ.get("PATH", "")
    extra = []
    if sys.platform == "darwin" and shutil.which("brew"):
        for formula in ("jbig2enc", "jbig2dec", "jpeg-turbo", "jpeg", "leptonica"):
            try:
                prefix = _run(["brew", "--prefix", formula]).strip()
            except (subprocess.CalledProcessError, OSError):
                continue
            extra.append(os.path.join(prefix, "bin"))
    if sys.platform == "win32":
        extra.extend([
            r"C:\msys64\ucrt64\bin",
            r"C:\msys64\mingw64\bin",
            "/ucrt64/bin",
            "/mingw64/bin",
        ])
    parts = extra + path.split(os.pathsep)
    suffixes = [""] if sys.platform != "win32" else ["", ".exe"]
    for directory in parts:
        if not directory:
            continue
        for suffix in suffixes:
            candidate = os.path.join(directory, name + suffix)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                found.append(os.path.abspath(candidate))
    # shutil.which as a last resort (respects PATHEXT)
    wh = shutil.which(name)
    if wh:
        found.append(os.path.abspath(wh))
    # unique, keep order
    out, seen = [], set()
    for item in found:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _copy_file(src, dest_dir, name=None):
    os.makedirs(dest_dir, exist_ok=True)
    name = name or os.path.basename(src)
    dest = os.path.join(dest_dir, name)
    if os.path.abspath(src) != os.path.abspath(dest):
        shutil.copy2(src, dest)
    os.chmod(dest, 0o755)
    return dest


def _linux_deps(path):
    """Return (soname, absolute path) for non-glibc shared libraries."""
    try:
        text = _run(["ldd", path])
    except (subprocess.CalledProcessError, OSError) as err:
        raise SystemExit("ldd failed for %s: %s" % (path, err))
    deps = []
    for line in text.splitlines():
        line = line.strip()
        if "=>" not in line:
            continue
        soname, rest = line.split("=>", 1)
        soname = soname.strip()
        rest = rest.strip()
        if soname in LINUX_SKIP_SONAMES or soname.startswith(LINUX_SKIP_PREFIXES):
            continue
        if rest == "not found" or not rest:
            raise SystemExit("%s needs %s, which was not found" % (path, soname))
        lib = rest.split()[0]
        if not os.path.isfile(lib):
            continue
        deps.append((soname, os.path.realpath(lib)))
    return deps


def _stage_linux(tools, dest_bin):
    if not shutil.which("patchelf"):
        raise SystemExit("patchelf is required to set RPATH on the bundled libraries")
    copied = {}
    staged = []

    def ensure_lib(soname, path):
        if soname in copied:
            return
        copied[soname] = path
        _copy_file(path, dest_bin, soname)
        for sub_soname, sub_path in _linux_deps(path):
            ensure_lib(sub_soname, sub_path)

    for name, src in tools:
        dest = _copy_file(src, dest_bin, name)
        staged.append(dest)
        for soname, path in _linux_deps(src):
            ensure_lib(soname, path)
    for fn in os.listdir(dest_bin):
        full = os.path.join(dest_bin, fn)
        if not os.path.isfile(full):
            continue
        subprocess.check_call(["patchelf", "--set-rpath", "$ORIGIN", full])
    return staged


def _otool_deps(path):
    text = _run(["otool", "-L", path])
    deps = []
    for line in text.splitlines()[1:]:
        item = line.strip().split(" (", 1)[0].strip()
        if item:
            deps.append(item)
    return deps


def _resolve_macho(origin, dep):
    if dep.startswith("@loader_path/") or dep.startswith("@executable_path/"):
        rel = dep.split("/", 1)[1]
        return os.path.normpath(os.path.join(os.path.dirname(origin), rel))
    if dep.startswith("@rpath/"):
        # Homebrew records the real path in LC_RPATH; otool -l is the fallback.
        name = dep.split("/", 1)[1]
        text = _run(["otool", "-l", origin])
        rpaths = []
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if "path " in line and "LC_RPATH" not in line and "(offset" in line:
                token = line.strip().split()
                # "path /opt/homebrew/lib (offset 12)"
                if token and token[0] == "path":
                    rpaths.append(token[1])
        for rp in rpaths:
            candidate = os.path.join(rp, name)
            if os.path.isfile(candidate):
                return candidate
        # last resort: next to the referring binary
        candidate = os.path.join(os.path.dirname(origin), name)
        if os.path.isfile(candidate):
            return candidate
        raise SystemExit("could not resolve %s from %s" % (dep, origin))
    return dep


def _stage_macos(tools, dest_bin):
    copied = {}

    def ensure(path):
        path = os.path.realpath(path)
        if path in copied:
            return copied[path]
        name = os.path.basename(path)
        if name in copied.values():
            name = "%d-%s" % (len(copied), name)
        dest = _copy_file(path, dest_bin, name)
        copied[path] = name
        subprocess.check_call(["install_name_tool", "-id", "@loader_path/%s" % name, dest])
        for dep in _otool_deps(path):
            if dep.startswith("/usr/lib/") or dep.startswith("/System/"):
                continue
            if dep.startswith("@"):
                resolved = _resolve_macho(path, dep)
            else:
                resolved = dep
            if not os.path.isfile(resolved):
                raise SystemExit("%s needs missing library %s" % (path, dep))
            dep_name = ensure(resolved)
            subprocess.check_call([
                "install_name_tool", "-change", dep, "@loader_path/%s" % dep_name, dest])
        if shutil.which("codesign"):
            subprocess.check_call(["codesign", "--force", "--sign", "-", dest])
        return name

    for _name, src in tools:
        ensure(src)
        # the tool must be invokable as jbig2 / jbig2dec / jpegtran
        base = _name
        current = os.path.join(dest_bin, os.path.basename(os.path.realpath(src)))
        wanted = os.path.join(dest_bin, base)
        if os.path.abspath(current) != os.path.abspath(wanted):
            if os.path.exists(wanted):
                os.remove(wanted)
            os.rename(current, wanted)
            # id was the old basename; point it at the new one
            subprocess.check_call(["install_name_tool", "-id", "@loader_path/%s" % base, wanted])
            if shutil.which("codesign"):
                subprocess.check_call(["codesign", "--force", "--sign", "-", wanted])


def _pe_imports(path):
    objdump = shutil.which("objdump") or shutil.which("x86_64-w64-mingw32-objdump")
    if not objdump:
        raise SystemExit("objdump is required to collect Windows DLL dependencies of %s" % path)
    text = _run([objdump, "-p", path])
    dlls = []
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("dll name:"):
            dlls.append(line.split(":", 1)[1].strip())
    return dlls


def _find_dll(name, search_dirs):
    for directory in search_dirs:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate):
            return candidate
    wh = shutil.which(name)
    if wh:
        return wh
    return None


def _stage_windows(tools, dest_bin):
    search = []
    for _name, src in tools:
        search.append(os.path.dirname(src))
    search.extend([
        r"C:\msys64\ucrt64\bin", r"C:\msys64\mingw64\bin",
        "/ucrt64/bin", "/mingw64/bin",
    ])
    copied = set()

    def ensure(path):
        path = os.path.abspath(path)
        name = os.path.basename(path)
        key = name.lower()
        if key in copied:
            return
        copied.add(key)
        _copy_file(path, dest_bin, name)
        for dll in _pe_imports(path):
            low = dll.lower()
            if low in WINDOWS_SKIP or low.startswith("api-ms-win-") or low.startswith("ext-ms-"):
                continue
            found = _find_dll(dll, search + [dest_bin])
            if not found:
                raise SystemExit("%s needs %s, which was not found" % (path, dll))
            ensure(found)

    for name, src in tools:
        ensure(src)
        dest_name = name + ".exe" if not src.lower().endswith(".exe") else os.path.basename(src)
        if not dest_name.lower().endswith(".exe"):
            dest_name += ".exe"
        current = os.path.join(dest_bin, os.path.basename(src))
        wanted = os.path.join(dest_bin, name + ".exe")
        if os.path.abspath(current) != os.path.abspath(wanted) and os.path.isfile(current):
            if os.path.exists(wanted):
                os.remove(wanted)
            os.rename(current, wanted)


def _write_notices(dest_lic):
    os.makedirs(dest_lic, exist_ok=True)
    notice = os.path.join(dest_lic, "NOTICES.txt")
    with open(notice, "w", encoding="utf-8") as fh:
        fh.write("""\
Third-party programs bundled so Hard Crop on Save can keep JPEG and JBIG2
images in their original encoding. The matching license texts are in this
folder.

jbig2 (jbig2enc)  Apache License 2.0. See jbig2enc-COPYING.txt,
                  Apache-2.0.txt, and jbig2enc-PATENTS.txt.
                  It is linked with Leptonica (2-clause BSD, leptonica-license.txt).

jpegtran          libjpeg-turbo, BSD-style. See libjpeg-turbo-LICENSE.md.

jbig2dec          GNU Affero General Public License v3 or later.
                  See jbig2dec-LICENSE.txt and jbig2dec-COPYING.txt.
                  kroppy itself is GPL-3.0-or-later. Shipping this AGPL
                  decoder in the same application means the combined binary
                  should be offered under the AGPL, including source, when
                  you distribute it. PyMuPDF, which kroppy already bundles,
                  is also AGPL unless a commercial license is used.
                  An alternative is to decode JBIG2 with that existing
                  PyMuPDF/MuPDF decoder and not ship jbig2dec separately.
                  The encoder (jbig2enc) would still be Apache-2.0.

Other shared libraries copied next to these programs (libpng, zlib, libtiff,
and similar) keep the license terms of the build that produced them. On
Debian/Ubuntu those texts are the *.copyright files copied here when present.
""")
    if os.path.isdir(LICENSE_SRC):
        for fn in os.listdir(LICENSE_SRC):
            src = os.path.join(LICENSE_SRC, fn)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(dest_lic, fn))
    for pkg in ("jbig2", "jbig2dec", "libjpeg-turbo-progs", "libjpeg-turbo8",
                "libleptonica6", "libleptonica-dev", "liblept5", "libleptonica5",
                "libpng16-16", "libpng16-16t64", "zlib1g", "libtiff6", "libjbig2enc0",
                "libjbig2enc0t64", "libjbig2dec0"):
        copyright_file = os.path.join("/usr/share/doc", pkg, "copyright")
        if os.path.isfile(copyright_file):
            shutil.copy2(copyright_file, os.path.join(dest_lic, pkg + ".copyright"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", default="packaging/codecs")
    args = parser.parse_args()
    dest = os.path.abspath(args.dest)
    dest_bin = os.path.join(dest, "bin")
    dest_lic = os.path.join(dest, "licenses")
    if os.path.isdir(dest_bin):
        shutil.rmtree(dest_bin)
    os.makedirs(dest_bin, exist_ok=True)

    tools = []
    for name in TOOLS:
        found = _which_all(name)
        if not found:
            raise SystemExit("could not find %s on PATH" % name)
        tools.append((name, found[0]))
        print("using %s: %s" % (name, found[0]))

    if sys.platform.startswith("linux"):
        _stage_linux(tools, dest_bin)
    elif sys.platform == "darwin":
        _stage_macos(tools, dest_bin)
    elif sys.platform == "win32":
        _stage_windows(tools, dest_bin)
    else:
        raise SystemExit("unsupported platform %s" % sys.platform)
    _write_notices(dest_lic)
    print("staged codecs in %s" % dest)


if __name__ == "__main__":
    main()
