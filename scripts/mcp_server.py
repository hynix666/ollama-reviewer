"""MCP stdio server exposing the reviewer as callable tools.

JSON-RPC 2.0 over stdin/stdout is implemented directly rather than via the MCP
SDK, because that SDK is a PyPI package and this project is standard-library
only. The protocol surface actually needed is small: initialize, tools/list,
tools/call, ping.

**Nothing may write to stdout except JSON-RPC frames.** A stray print corrupts
the stream and the client disconnects, so diagnostics go to stderr and the
engine is called for its return value rather than its output.

Register it in ~/.claude.json:

    "mcpServers": {
      "ollama-reviewer": {
        "command": "python",
        "args": ["<abs path>/scripts/mcp_server.py"]
      }
    }
"""

import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cli  # noqa: E402
import collect  # noqa: E402
import ollama_client as oc  # noqa: E402
import prompts  # noqa: E402
import render  # noqa: E402
import review  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "ollama-reviewer"
SERVER_VERSION = "1.0.0"

_FOCUS = sorted(prompts.FOCUS_AREAS)

_COMMON_PROPS = {
    "focus": {
        "type": "array",
        "items": {"type": "string", "enum": _FOCUS},
        "description": "Areas to review. Defaults to all but 'design'.",
    },
    "adversarial": {
        "type": "boolean",
        "description": "Assume the design is wrong and attack its assumptions.",
    },
    "instructions": {
        "type": "string",
        "description": "Extra steering, e.g. 'focus on the retry loop'.",
    },
    "models": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Review with several models and mark which ones agree. "
        "Two models roughly double the time.",
    },
    "format": {
        "type": "string",
        "enum": ["markdown", "json"],
        "description": "Result format. Defaults to markdown.",
    },
}


def _schema(extra_props, required):
    props = dict(_COMMON_PROPS)
    props.update(extra_props)
    return {"type": "object", "properties": props, "required": required}


TOOLS = [
    {
        "name": "ollama_review_file",
        "description": (
            "Review one or more files on disk with a local Ollama model. Returns "
            "findings the CALLER must verify - the model is advisory only and is "
            "wrong a substantial fraction of the time. Never edit code solely "
            "because a finding says so."
        ),
        "inputSchema": _schema(
            {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "File paths to review. Binaries are rejected.",
                }
            },
            ["paths"],
        ),
    },
    {
        "name": "ollama_review_code",
        "description": (
            "Review a snippet of code passed as a string. Accepts a unified diff "
            "too, which is split at file boundaries. Findings are advisory and "
            "must be verified by the caller."
        ),
        "inputSchema": _schema(
            {
                "code": {"type": "string", "description": "The code or diff to review."},
                "label": {
                    "type": "string",
                    "description": "A name for the snippet, shown in findings.",
                },
            },
            ["code"],
        ),
    },
    {
        "name": "ollama_review_diff",
        "description": (
            "Review a git diff: uncommitted by default, staged, or against a ref. "
            "Findings are advisory and must be verified by the caller."
        ),
        "inputSchema": _schema(
            {
                "cwd": {"type": "string", "description": "Repository directory."},
                "ref": {"type": "string", "description": "Compare REF...HEAD."},
                "staged": {"type": "boolean", "description": "Review staged changes."},
            },
            [],
        ),
    },
    {
        "name": "ollama_list_models",
        "description": (
            "Check the local Ollama server and list installed models, with the "
            "configured and resolved review model."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _ok(text):
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _err(text):
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _error_text(err):
    return "Review unavailable.\n\nProblem: %s\n\nHow to fix: %s\n\nThe review did " \
           "not run; nothing has been verified." % (
               err.get("detail", "unknown"), err.get("remedy", "(none recorded)")
           )


def _validated_focus(args):
    focus = args.get("focus") or prompts.DEFAULT_FOCUS
    bad = [f for f in focus if f not in prompts.FOCUS_AREAS]
    if bad:
        raise ValueError(
            "Unknown focus area(s): %s. Valid: %s" % (", ".join(bad), ", ".join(_FOCUS))
        )
    if args.get("adversarial") and "design" not in focus:
        focus = list(focus) + ["design"]
    return focus


def _run(cfg, notes, inp, args):
    """Shared tail of every review tool."""
    focus = _validated_focus(args)
    models = review.resolve_models(cfg, list(args.get("models") or []), notes)
    opts = review.ReviewOptions(
        adversarial=bool(args.get("adversarial")),
        instructions=args.get("instructions"),
    )
    result = review.run_review(cfg, models, inp, focus, opts, notes)
    if args.get("format") == "json":
        return _ok(json.dumps(result, indent=2))
    return _ok(render.to_markdown(result))


def call_tool(name, args):
    """Dispatch one tool call. Never raises; failures come back as isError."""
    args = args or {}
    cfg, notes = cli.load_config()
    try:
        if name == "ollama_list_models":
            installed = oc.list_models(cfg)
            resolved = None
            try:
                resolved, rnotes = oc.resolve_model(cfg, None, {
                    m.get("name") for m in installed if m.get("name")
                })
                notes += rnotes
            except oc.OllamaError as e:
                notes.append("%s %s" % (e.detail, e.remedy))
            payload = {
                "status": "ok",
                "base_url": cfg["base_url"],
                "configured_model": cfg["model"],
                "resolved_model": resolved,
                "fallback_models": cfg.get("fallback_models"),
                "notes": notes,
                "models": [
                    {
                        "name": m.get("name"),
                        "size_h": cli.human_size(m.get("size")),
                        "context": (m.get("details") or {}).get("context_length", "?"),
                    }
                    for m in sorted(installed, key=lambda x: x.get("name") or "")
                ],
            }
            return _ok(render.status_markdown(payload))

        if name == "ollama_review_file":
            paths = args.get("paths") or []
            if not paths:
                return _err("No paths given. Pass 'paths' as a list of file paths.")
            return _run(cfg, notes, collect.from_files(cfg, paths), args)

        if name == "ollama_review_code":
            return _run(
                cfg,
                notes,
                collect.from_text(
                    cfg, args.get("code") or "", args.get("label") or "snippet"
                ),
                args,
            )

        if name == "ollama_review_diff":
            return _run(
                cfg,
                notes,
                collect.from_git(
                    cfg,
                    ref=args.get("ref"),
                    staged=bool(args.get("staged")),
                    cwd=args.get("cwd") or ".",
                ),
                args,
            )

        return _err("Unknown tool: %s" % name)

    except ValueError as e:
        return _err(str(e))
    except collect.InputError as e:
        return _err(_error_text(e.to_dict()))
    except oc.OllamaError as e:
        return _err(_error_text(e.to_dict()))
    except review.ReviewFailure as e:
        return _err(_error_text(e.error))
    except Exception as e:  # never take the client down with us
        sys.stderr.write(traceback.format_exc())
        return _err("Internal error in the review server: %r" % (e,))


def dispatch(msg):
    """Handle one JSON-RPC message. Returns a response dict, or None for a notification."""
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if msg_id is None:  # notification: acknowledge by staying silent
        return None

    def result(payload):
        return {"jsonrpc": "2.0", "id": msg_id, "result": payload}

    def error(code, message):
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    if method == "initialize":
        return result(
            {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        )
    if method == "ping":
        return result({})
    if method == "tools/list":
        return result({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        if not name:
            return error(-32602, "tools/call requires a tool name")
        return result(call_tool(name, params.get("arguments")))

    return error(-32601, "Method not found: %s" % method)


def serve(stdin=None, stdout=None):
    """Read newline-delimited JSON-RPC from stdin, write responses to stdout."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except (ValueError, TypeError):
            out = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
        else:
            try:
                out = dispatch(msg)
            except Exception as e:
                sys.stderr.write(traceback.format_exc())
                out = {
                    "jsonrpc": "2.0",
                    "id": msg.get("id"),
                    "error": {"code": -32603, "message": "Internal error: %r" % (e,)},
                }
        if out is None:
            continue
        stdout.write(json.dumps(out) + "\n")
        stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(serve())
