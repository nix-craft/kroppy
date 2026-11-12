# Control socket

kroppy can listen for commands on a local Unix socket. Nothing listens unless you ask.

```
kroppy --control
kroppy --control /tmp/kroppy.sock
KROPPY_CONTROL=/tmp/kroppy.sock kroppy file.pdf
```

`--control` with no path uses `$XDG_RUNTIME_DIR/kroppy-<pid>.sock`, or `/tmp/kroppy-$USER.sock` when that directory is missing. The path is printed to stderr at startup. The socket is mode 0600 and is not a network port.

Each request is one JSON object per line:

```
{"id": 1, "cmd": "state", "args": {}}
```

The response is one JSON object:

```
{"id": 1, "ok": true, "result": {}}
{"id": 1, "ok": false, "error": "timeout"}
```

Commands run on the GUI thread. `wait_idle` does not freeze the window: the client blocks until the current page has been scanned, no P0 or P1 job is queued or running, and no preview is still pending, or until `timeout` seconds (default 30).

| Command | Args | Result |
| --- | --- | --- |
| `ping` | | `{"pong": true}` |
| `state` | | file, page count, current page, zoom, window size, selection, scheduler queues, worker, error limit, toggles, and `padding` (the live Padding field) |
| `open` | `path` | pages opened |
| `goto` | `page` | 1-based page |
| `zoom` | `factor` or `"fit"` | zoom factor |
| `scroll_to_image` | `xref` | placement rectangle |
| `analysis` | `page` optional | the analysis JSON (one page, or the whole document), including candidates and `chosen_target` |
| `select_image` | `page`, `xref` | selects that row |
| `set_target` | `xref`, `target` | `indexed16` and `indexed-16` are the same target |
| `set_option` | `name`, `value` | `mean_error`, `preview`, `analyze_other_pages` |
| `set_padding` | `value` | writes the Padding field. The next trim reads that text, not a value saved at startup |
| `trim` | `scope` | `current` trims the selection, `page` trims every selection on this page, `every` trims each page on its own. Uses the Padding field as it is now |
| `trim_copier` | `scope` | same scopes, with Copier trim on |
| `wait_idle` | `timeout` optional | `{"idle": true}` |
| `grab_view` | `path` | PNG of the page view, overlays included |
| `grab_window` | `path` | PNG of the window |
| `export_image` | `xref`, `which`, `path` | `original` is the full-resolution decode. `preview` is a full-resolution encode/decode of the chosen target. `preview_display` is the pixmap drawn on the page. The result includes width, height, and error stats |
| `log_tail` | `n` optional | last activity-log lines |
| `quit` | | closes the window |

`kroppyctl` sends one command and prints the JSON response. With a single `kroppy-*.sock` in the runtime directory or `/tmp`, `--sock` can be omitted.

```
python -m krop.ctl --sock "$SOCK" state
python -m krop.ctl set_target 59 indexed16
python -m krop.ctl export_image 59 preview_display /tmp/p.png
kroppyctl wait_idle 20
kroppyctl set_padding 0
kroppyctl trim page
kroppyctl set_padding 12
kroppyctl trim every
kroppyctl trim_copier current
```
