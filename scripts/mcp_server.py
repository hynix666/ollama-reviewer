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

import collect  # noqa: E402
import config  # noqa: E402
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
        "type": ["array", "string"],
        "items": {"type": "string", "enum": _FOCUS},
        "description": "Areas to review, as a list or a comma-separated string. "
        "Defaults to all but 'design'.",
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
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


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

KNOWN_TOOLS = frozenset(t["name"] for t in TOOLS)


def _schema_for(name):
    """The advertised inputSchema for a tool, or None if unadvertised."""
    for t in TOOLS:
        if t["name"] == name:
            return t.get("inputSchema")
    return None


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
    focus, err = prompts.resolve_focus(
        args.get("focus"), adversarial=bool(args.get("adversarial")))
    if err:
        raise ValueError(err)
    return focus


def _run(cfg, notes, inp, args):
    """Shared tail of every review tool."""
    focus = _validated_focus(args)
    opts = review.ReviewOptions(
        adversarial=bool(args.get("adversarial")),
        instructions=args.get("instructions"),
    )
    result = review.run_pipeline(
        cfg, list(args.get("models") or []), inp, focus, opts, notes)
    if args.get("format") == "json":
        return _ok(json.dumps(result, indent=2))
    return _ok(render.to_markdown(result))


_JSON_TYPES = {"string": (str,), "boolean": (bool,), "array": (list,), "number": (int, float)}
_JSON_TYPE_NAMES = {"string": "a string", "boolean": "a JSON boolean", "array": "an array", "number": "a number"}


def _check_types(name, args):
    """Validate tool arguments against the very schema tools/list advertises.

    One source of truth: rules are derived from TOOLS, so the advertised
    schema and its enforcement cannot drift apart. The CLI exits 2 at the
    parser for contradictory input (_Once, F7); the MCP front end has no
    parser, so wrong-typed JSON used to be silently reinterpreted:
    `models: "m1,m2"` became five one-character model names via list(str),
    `staged: "yes"` became True via bool(str), and a misspelled argument
    was dropped without a trace. JSON null is accepted everywhere as
    "unset" - the same-as-absent behavior these arguments always had.
    """
    schema = _schema_for(name)
    if schema is None:
        return
    unknown = sorted(set(args) - set(schema["properties"]))
    if unknown:
        raise ValueError(
            "Tool %s: unknown argument(s) %s. Valid: %s."
            % (name, ", ".join(unknown), ", ".join(sorted(schema["properties"]))))
    for key, spec in schema["properties"].items():
        value = args.get(key)
        if value is None:
            continue
        allowed = spec.get("type")
        kinds = [allowed] if isinstance(allowed, str) else list(allowed or [])
        py = tuple(t for k in kinds for t in _JSON_TYPES.get(k, ()))
        if py and not isinstance(value, py):
            names = ", ".join(_JSON_TYPE_NAMES.get(k, k) for k in kinds)
            raise ValueError(
                "Tool %s: argument '%s' must be %s, got %s."
                % (name, key, names, type(value).__name__))
        if isinstance(value, list):
            item = spec.get("items", {})
            enums = item.get("enum")
            for i in value:
                if not isinstance(i, str):
                    raise ValueError(
                        "Tool %s: argument '%s' must be an array of strings; got %r."
                        % (name, key, i))
                if enums and i not in enums:
                    raise ValueError(
                        "Tool %s: argument '%s' has invalid value %r. Valid: %s."
                        % (name, key, i, ", ".join(enums)))
        enum = spec.get("enum")
        if enum and value not in enum:
            raise ValueError(
                "Tool %s: argument '%s' must be one of %s, got %r."
                % (name, key, ", ".join(map(repr, enum)), value))


def call_tool(name, args):
    """Dispatch one tool call. Never raises; failures come back as isError."""
    args = {} if args is None else args
    cfg, notes = config.load_config()
    try:
        if not isinstance(args, dict):
            raise ValueError(
                "Tool %s: 'arguments' must be a JSON object, got %s."
                % (name, type(args).__name__))
        if name not in KNOWN_TOOLS:
            raise ValueError(
                "Unknown tool: %s. Available: %s."
                % (name, ", ".join(sorted(KNOWN_TOOLS))))
        _check_types(name, args)
        if name == "ollama_list_models":
            return _ok(render.status_markdown(
                oc.status_snapshot(cfg, None, notes))
            )

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


def _no_duplicate_keys(pairs):
    """json object_pairs_hook: a duplicated JSON key is ambiguous - RFC 8259
    lets implementations pick any behavior, and Python's dict builder quietly
    keeps the last (`{"staged": false, "staged": true}` would review staged
    changes when the caller said not to). Reject instead: parse error."""
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate object key: %r" % k)
        out[k] = v
    return out


def serve(stdin=None, stdout=None):
    """Read newline-delimited JSON-RPC from stdin, write responses to stdout."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line, object_pairs_hook=_no_duplicate_keys)
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
        try:
            stdout.write(json.dumps(out) + "\n")
            stdout.flush()
        except (BrokenPipeError, OSError, ValueError):
            # The client went away mid-write. That is a normal shutdown, not a
            # fault: exit quietly rather than dumping a traceback.
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(serve())
