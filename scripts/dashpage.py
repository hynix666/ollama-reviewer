#!/usr/bin/env python3
"""Presentation half of the dashboard: CSS, chips, and the HTML template.

render(data, refresh) turns a data dict gathered by dashboard.py into the
standalone HTML page. Kept separate from data gathering so each module
stays within the project's line budget. Pure standard library.
"""

import html
import time

E = html.escape

CSS = """
:root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--fg:#e6edf3;--dim:#8b949e;
--green:#3fb950;--amber:#d29922;--red:#f85149;--accent:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 ui-monospace,'Cascadia Code',Consolas,monospace;padding:32px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;color:var(--accent);
margin:28px 0 10px;text-transform:uppercase;letter-spacing:.08em}
.tag{color:var(--dim);margin:0 0 18px}.chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px}
.chip{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:4px 10px;font-size:12px}.chip b{color:var(--accent)}
.chip.up b{color:var(--green)}.chip.down b{color:var(--red)}.chip.warn b{color:var(--amber)}
.warn{border:1px solid var(--amber);background:rgba(210,153,34,.08);color:var(--amber);
padding:10px 14px;border-radius:6px;margin:14px 0 4px;font-size:13px}
table{border-collapse:collapse;width:100%;background:var(--panel);
border:1px solid var(--line);border-radius:6px;overflow:hidden}
td,th{padding:5px 10px;border-bottom:1px solid var(--line);text-align:left;font-size:12.5px}
th{color:var(--dim);font-weight:600}td:last-child{color:var(--dim)}tr:last-child td{border-bottom:none}
.pill{display:inline-block;min-width:44px;text-align:center;border-radius:10px;font-size:11px;padding:1px 8px;font-weight:700}
.PASS{background:rgba(63,185,80,.15);color:var(--green)}.SKIP{background:rgba(210,153,34,.15);color:var(--amber)}.FAIL{background:rgba(248,81,73,.18);color:var(--red)}
.hash{color:var(--accent)}ul{margin:0;padding-left:20px;color:var(--dim)}
li{margin:3px 0}li b{color:var(--fg);font-weight:600}
.run-title a{color:var(--fg);text-decoration:none}.run-title a:hover{color:var(--accent)}
.jpill{display:inline-block;border-radius:8px;font-size:10.5px;padding:0 6px;margin:1px 2px 1px 0;border:1px solid transparent}
.jp-success{background:rgba(63,185,80,.15);color:var(--green);border-color:rgba(63,185,80,.4)}
.jp-failure,.jp-cancelled,.jp-timed_out{background:rgba(248,81,73,.18);color:var(--red);border-color:rgba(248,81,73,.4)}.jp-skipping{background:rgba(210,153,34,.15);color:var(--amber)}.jp-other{background:rgba(139,148,158,.15);color:var(--dim)}
.run-note{color:var(--amber);font-size:12.5px;margin:6px 0 0}
footer{margin-top:28px;color:var(--dim);font-size:12px;border-top:1px solid var(--line);padding-top:12px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:24px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
"""

JOBS = [
    ("test", "6-OS x Python 3.8-3.13 matrix, full offline suite (65 checks)"),
    ("mutation-guards", "--mutate on Linux + Windows: all 24 registry mutations applied, every guard must fire"),
    ("mcp-stdio", "dedicated runner: spawned-process JSON-RPC end-to-end, --check mcp"),
    ("installers", "bash -n, PowerShell parse, executable bit, LF endings"),
]

HARDENING = [
    "actions pinned to <b>release SHAs</b>, not mutable tags",
    "per-job <b>least-privilege</b> token grants (contents: read / none)",
    "concurrency namespaced by <b>workflow x event x ref</b>",
    "no dependency cache: suite is <b>pure standard library</b>, documented in ci.yml",
    "guard decay fails loudly on both platforms: <b>24-mutation registry</b> via selftest --mutate",
]

_JP = {
    "success": "jp-success", "failure": "jp-failure", "cancelled": "jp-cancelled",
    "timed_out": "jp-timed_out", "skipping": "jp-skipping",
}


def jpill(label):
    return '<span class="jpill %s">%s</span>' % (_JP.get(label, "jp-other"), E(label))


def _marker_chip(ms):
    """One mutation_marker.status() dict as a chip; warn styling for the
    states a maintainer should look at (unparseable holder or stranded)."""
    if ms["state"] == "absent":
        return '<span class="chip">mutation marker: <b>none</b></span>'
    if ms["state"] == "in flight" and ms["holder"] is not None:
        return ('<span class="chip">mutation marker: <b>in flight</b> '
                '(pid %d, age %ds)</span>' % (ms["holder"], ms["age_s"]))
    if ms["state"] == "in flight":  # unparseable: suites gate until the backstop
        return ('<span class="chip warn">mutation marker: <b>in flight</b> '
                '(unparseable holder, age %ds)</span>' % ms["age_s"])
    if ms["holder"] is None:  # stranded: expired, unparseable content
        return ('<span class="chip warn">mutation marker: <b>STRANDED</b> '
                '(unparseable, age %ds)</span>' % ms["age_s"])
    if ms["holder_alive"] is False:
        return ('<span class="chip warn">mutation marker: <b>STRANDED</b> '
                '(pid %d dead, age %ds)</span>' % (ms["holder"], ms["age_s"]))
    return ('<span class="chip warn">mutation marker: <b>STRANDED</b> '
            '(pid %d past the age backstop)</span>' % ms["holder"])


def render(d, refresh=None):
    """Build the page HTML from gathered data d (see dashboard.gather)."""
    rows, mods = d["rows"], d["mods"]
    npass = sum(1 for r in rows if r[1] == "PASS")
    nskip = sum(1 for r in rows if r[1] == "SKIP")
    nfail = sum(1 for r in rows if r[1] == "FAIL")

    mode = "LIVE" if d["live"] else "offline"
    mode_chip = ("selftest (%s): <b>%d</b> pass / %d fail%s"
                 % (mode, npass, nfail, (" / %d skip" % nskip) if nskip else ""))
    server_chip = ('<span class="chip up">ollama server: <b>up - %d models</b></span>' % d["n_models"]
                   if d["up"] else
                   '<span class="chip down">ollama server: <b>NOT REACHABLE</b></span>')
    banner = ('  <p class="warn">&#9888; live run attempted but the Ollama server is not '
              "reachable - the FAIL rows below reflect that, not a code regression. "
              "Start the server and re-run: <b>python scripts/dashboard.py --live</b></p>\n"
              ) if d["live"] and not d["up"] else ""
    for note in (d.get("rows_note"), d.get("mut_note")):
        if note:  # a child produced no verdict: the cycle explains itself
            banner += '  <p class="warn">&#9888; %s</p>\n' % E(note)

    check_rows = "\n".join(
        "      <tr><td>%s</td><td><span class=\"pill %s\">%s</span></td><td>%s</td></tr>"
        % (E(name), status, status, E(detail))
        for name, status, detail in rows
    )
    mod_rows = "\n".join(
        "        <tr><td>%s</td><td>%d</td></tr>" % (E(n), c) for n, c in mods
    )
    commit_rows = "\n".join(
        '        <tr><td class="hash">%s</td><td>%s</td></tr>' % (h, s) for h, s in d["commits"]
    )
    job_rows = "\n".join(
        "        <tr><td><b>%s</b></td><td>%s</td></tr>" % (E(j), E(x)) for j, x in JOBS
    )
    hard = "\n".join("      <li>%s</li>" % h for h in HARDENING)
    mut_ok, mut_total = d["mut_ok"], d["mut_total"]
    if mut_ok:
        mut_chip = "mutations: <b>%d</b> / %d caught" % (mut_total, mut_total)
    elif d.get("mut_note"):  # no verdict: a degraded cycle, not a regression
        mut_chip = "mutations: <b>NO VERDICT</b> - degraded cycle, see the warning"
    else:
        mut_chip = "mutations: <b>REGRESSION</b> - not all of %d caught" % mut_total
    marker_chip = _marker_chip(d["marker"])
    refresh_tag = '<meta http-equiv="refresh" content="%d">' % refresh if refresh else ""
    auto_note = " &middot; auto-refreshes every %ds" % refresh if refresh else ""
    gen = d.get("generation")  # (cycle, reason) - watch() records why it ran
    gen_note = " (cycle %d, %s)" % gen if gen else ""

    if d["ci_note"] is not None:  # gh missing, unauthenticated, or offline: degrade honestly
        ci_section = '  <h2>GitHub Actions</h2>\n  <p class="run-note">%s</p>\n' % E(d["ci_note"])
    elif d["ci"]:
        run_rows = "\n".join(
            '        <tr><td class="run-title"><a href="%s">%s</a></td>'
            '<td class="hash">%s</td><td>%s</td>'
            '<td>%s %s</td><td>%s</td></tr>'
            % (E(r["url"]), E(r["title"]), r["sha"], E(r["event"]),
               jpill(r["conclusion"] or r["status"]), E(r["created"]),
               "".join(jpill(j[1]) for j in r["jobs"]) or "&mdash;")
            for r in d["ci"])
        ci_section = ('  <h2>GitHub Actions (live via gh)</h2>\n  <table>\n'
                      '    <tr><th>run</th><th>sha</th><th>event</th>'
                      '<th>result</th><th>jobs</th></tr>\n%s\n  </table>\n' % run_rows)
    else:
        ci_section = ''

    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
%s
<title>ollama-reviewer - project dashboard</title>
<style>%s</style>
</head>
<body>
  <h1>ollama-reviewer</h1>
  <p class="tag">Pure-stdlib Ollama code reviewer: CLI + MCP server, self-verifying test suite.</p>
  <div class="chips">
    <span class="chip">%s</span>
%s
    <span class="chip">%s</span>
%s
    <span class="chip">modules: <b>%d</b> files, %d lines</span>
    <span class="chip">working tree: <b>%s</b></span>
  </div>
%s
  <h2>Self-test (%s run%s)</h2>
  <table>
    <tr><th>check</th><th>result</th><th>detail</th></tr>
%s
  </table>

%s
  <div class="grid">
    <div>
      <h2>CI pipeline</h2>
      <table>
%s
      </table>
      <h2>Hardening</h2>
      <ul>
%s
      </ul>
    </div>
    <div>
      <h2>Modules</h2>
      <table>
        <tr><th>file</th><th>lines</th></tr>
%s
      </table>
      <h2>Recent commits</h2>
      <table>
%s
      </table>
    </div>
  </div>

  <footer>generated %s by scripts/dashboard.py%s - every number parsed from a fresh %s selftest run, git, the tree, and gh%s</footer>
</body>
</html>
""" % (
        refresh_tag, CSS, mode_chip, server_chip, mut_chip, marker_chip, len(mods),
        sum(n for _, n in mods), E(d["tree"]),
        banner, mode, " - real inference" if d["live"] else "",
        check_rows, ci_section, job_rows, hard, mod_rows, commit_rows,
        time.strftime("%Y-%m-%d %H:%M:%S"), gen_note, mode, auto_note,
    )
