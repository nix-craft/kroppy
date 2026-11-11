# -*- coding: utf-8 -*-
"""kroppyctl: send one command to a running kroppy control socket.

    python -m krop.ctl --sock PATH state
    python -m krop.ctl set_target 59 indexed16
    python -m krop.ctl export_image 59 preview_display /tmp/p.png
"""
import json
import socket
import sys

from krop.control import discover_sockets


_USAGE = {
    "ping": [],
    "state": [],
    "quit": [],
    "open": ["path"],
    "goto": ["page"],
    "zoom": ["factor"],
    "scroll_to_image": ["xref"],
    "select_image": ["page", "xref"],
    "set_target": ["xref", "target"],
    "set_option": ["name", "value"],
    "grab_view": ["path"],
    "grab_window": ["path"],
    "export_image": ["xref", "which", "path"],
    "analysis": ["page"],
    "wait_idle": ["timeout"],
    "log_tail": ["n"],
}
_OPTIONAL = {"analysis": 1, "wait_idle": 1, "log_tail": 1}


def build_args(cmd, rest):
    """Map positional CLI words onto the command's args object."""
    if cmd not in _USAGE:
        raise SystemExit("unknown command %s" % cmd)
    names = _USAGE[cmd]
    optional = _OPTIONAL.get(cmd, 0)
    need = len(names) - optional
    if len(rest) < need or len(rest) > len(names):
        expect = " ".join(names) if names else ""
        raise SystemExit("usage: kroppyctl %s %s" % (cmd, expect).rstrip())
    args = {}
    for name, value in zip(names, rest):
        if name in ("page", "xref", "n"):
            try:
                value = int(value)
            except ValueError:
                raise SystemExit("%s must be an integer" % name)
        elif name == "timeout":
            try:
                value = float(value)
            except ValueError:
                raise SystemExit("timeout must be a number")
        args[name] = value
    return args


def resolve_socket(path):
    if path:
        return path
    found = discover_sockets()
    if len(found) == 1:
        return found[0]
    if not found:
        raise SystemExit("no kroppy control socket found")
    raise SystemExit("more than one control socket:\n" + "\n".join(found))


def transact(path, cmd, args, timeout=60, req_id=1):
    """Send one request and return the response object."""
    payload = json.dumps({"id": req_id, "cmd": cmd, "args": args or {}}) + "\n"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall(payload.encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()
    if not buf:
        raise SystemExit("control socket closed without a response")
    line = buf.split(b"\n", 1)[0]
    return json.loads(line.decode("utf-8"))


def main(argv=None):
    from argparse import ArgumentParser
    parser = ArgumentParser(prog="kroppyctl", description=__doc__)
    parser.add_argument("--sock", help="control socket. Discovered when only one kroppy-*.sock exists")
    parser.add_argument("--timeout", type=float, default=60, help="seconds to wait for a response (default 60)")
    parser.add_argument("cmd", help="command name, for example state or set_target")
    parser.add_argument("rest", nargs="*", help="command arguments")
    args = parser.parse_args(argv)
    cmd_args = build_args(args.cmd, args.rest)
    path = resolve_socket(args.sock)
    timeout = float(args.timeout)
    if args.cmd == "wait_idle" and "timeout" in cmd_args:
        timeout = max(timeout, float(cmd_args["timeout"]) + 5.0)
    try:
        response = transact(path, args.cmd, cmd_args, timeout=timeout)
    except (socket.timeout, TimeoutError):
        sys.stderr.write("timed out waiting for %s\n" % path)
        return 1
    except OSError as exc:
        sys.stderr.write("%s\n" % exc)
        return 1
    json.dump(response, sys.stdout)
    sys.stdout.write("\n")
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
