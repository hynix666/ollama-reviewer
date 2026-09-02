"""HTTP client for a local Ollama server, with a typed error taxonomy.

Every failure mode surfaces as an OllamaError carrying a machine-readable `kind`,
a human `detail`, and an actionable `remedy`. Nothing escapes as a bare exception.
"""

import json
import socket
import time
import urllib.error
import urllib.request

RETRYABLE = {"oom", "loading", "http_5xx", "timeout", "malformed"}


class OllamaError(Exception):
    def __init__(self, kind, detail, remedy, status=None):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.remedy = remedy
        self.status = status

    @property
    def retryable(self):
        return self.kind in RETRYABLE

    def to_dict(self):
        return {
            "kind": self.kind,
            "detail": self.detail,
            "remedy": self.remedy,
            "status": self.status,
        }


def normalize_base_url(url):
    """Make a bind address usable as a connect address.

    OLLAMA_HOST is commonly set to 0.0.0.0 or :: so the server listens on every
    interface. Those are not routable destinations - on Windows, connecting to
    0.0.0.0 fails with WinError 10049 - so rewrite them to loopback.
    """
    url = (url or "").strip()
    if not url:
        return "http://127.0.0.1:11434"
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url
    for bind_addr, loopback in (("//0.0.0.0", "//127.0.0.1"), ("//[::]", "//[::1]")):
        if bind_addr in url:
            url = url.replace(bind_addr, loopback, 1)
    return url.rstrip("/")


def _timeout_error(phase):
    """A timeout while probing liveness means 'not there', not 'slow'."""
    if phase == "health":
        return OllamaError(
            "unreachable",
            "The Ollama server did not respond to a health check in time.",
            "Ollama is not running, or is listening elsewhere. Start it with: "
            "ollama serve",
        )
    return OllamaError(
        "timeout",
        "Request exceeded the configured timeout.",
        "Raise --timeout, narrow the review scope, or pick a smaller --model.",
    )


def _post(url, payload, timeout):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    return _send(req, timeout)


def _get(url, timeout, phase="request"):
    return _send(urllib.request.Request(url, method="GET"), timeout, phase)


def _send(req, timeout, phase="request"):
    """Perform the request, translating every transport failure into OllamaError.

    `phase="health"` means we are probing liveness on a short budget, so a timeout
    means the server is not there rather than that the work took too long.
    """
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail_body = ""
        try:
            detail_body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        low = detail_body.lower()
        if "memory" in low or "oom" in low or "resource" in low:
            raise OllamaError(
                "oom",
                "Ollama reports insufficient memory: %s" % detail_body.strip()[:200],
                "Free VRAM/RAM (`ollama ps`, then `ollama stop <model>`), or pass a "
                "smaller --model.",
                status=e.code,
            )
        if "loading" in low:
            raise OllamaError(
                "loading", "Model is still loading.", "Retry shortly.", status=e.code
            )
        kind = "http_5xx" if e.code >= 500 else "http_4xx"
        raise OllamaError(
            kind,
            "Ollama returned HTTP %s: %s"
            % (e.code, detail_body.strip()[:200] or "(empty body)"),
            "Check the `ollama serve` logs. A 404 here usually means the model name "
            "is wrong; a 400 means the request payload was rejected.",
            status=e.code,
        )
    except socket.timeout:
        raise _timeout_error(phase)
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, socket.timeout):
            raise _timeout_error(phase)
        raise OllamaError(
            "unreachable",
            "Cannot reach the Ollama server (%s)." % (reason,),
            "Ollama is not running, or is listening elsewhere. Start it with: "
            "ollama serve",
        )
    except Exception as e:
        raise OllamaError(
            "internal",
            "Unexpected transport failure: %r" % (e,),
            "Re-run with --debug for a traceback.",
        )

    if not raw.strip():
        raise OllamaError(
            "malformed",
            "Ollama returned an empty response body for %s." % req.full_url,
            "Transient; a retry usually clears it.",
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise OllamaError(
            "malformed",
            "Ollama returned non-JSON output from %s (%s): %s"
            % (req.full_url, e, raw[:200]),
            "Transient; a retry usually clears it. If it persists, the server may not "
            "be Ollama.",
        )


def list_models(cfg):
    """Return installed model records. Raises OllamaError if unreachable."""
    base = normalize_base_url(cfg["base_url"])
    data = _get(base + "/api/tags", cfg["connect_timeout_s"], phase="health")
    models = data.get("models")
    if not isinstance(models, list):
        raise OllamaError(
            "malformed",
            "Unexpected /api/tags payload shape.",
            "Confirm the endpoint really is an Ollama server.",
        )
    return models


def resolve_model(cfg, requested, available_names):
    """Pick a usable model, honouring the configured fallback chain.

    Accepts a bare family name such as "qwen3-coder" and resolves it to an
    installed tag. Returns (model_name, notes). Raises OllamaError when nothing
    in the chain is installed.
    """
    notes = []
    if requested:
        candidates = [requested]
    else:
        candidates = [cfg.get("model")] + list(cfg.get("fallback_models") or [])

    # A blank or non-string model name in config would otherwise surface as
    # "ollama pull None". Drop them before they reach the error message.
    candidates = [c for c in candidates if isinstance(c, str) and c.strip()]
    if not candidates:
        raise OllamaError(
            "model_missing",
            "No review model is configured.",
            'Set "model" in config.json, pass --model, or set OLLAMA_REVIEW_MODEL. '
            "Installed: %s" % (", ".join(sorted(available_names)) or "(none)"),
        )

    for cand in candidates:
        if cand.endswith(":cloud") and not cfg.get("allow_cloud_models"):
            raise OllamaError(
                "cloud_blocked",
                "Model %r is a cloud model and cloud use is disabled." % cand,
                'Set "allow_cloud_models": true in config.json to permit it.',
            )
        if cand in available_names:
            return cand, notes
        prefix_hits = sorted(n for n in available_names if n.split(":")[0] == cand)
        if prefix_hits:
            notes.append("Resolved %r to installed tag %r." % (cand, prefix_hits[0]))
            return prefix_hits[0], notes
        notes.append("Model %r is not installed." % cand)

    installed = ", ".join(sorted(available_names)) or "(none)"
    raise OllamaError(
        "model_missing",
        "None of the candidate models are installed (%s)."
        % ", ".join(c for c in candidates if c),
        "Install one with: ollama pull %s\nInstalled: %s" % (candidates[0], installed),
    )


CTX_MIN = 8192
CTX_MAX = 65536
CHARS_PER_TOKEN = 3.0  # deliberately pessimistic; code tokenizes denser than prose
RESERVE_TOKENS = 2048  # headroom for the model's own answer


def size_context(system, user):
    """Choose num_ctx large enough that the prompt is not silently truncated.

    Ollama drops whatever exceeds num_ctx without telling anyone. A review of a
    half-seen file still reports confident findings, so undersizing this is a
    silent-wrong-answer bug rather than a visible failure.
    """
    est = int((len(system) + len(user)) / CHARS_PER_TOKEN) + RESERVE_TOKENS
    return max(CTX_MIN, min(CTX_MAX, est))


def generate(
    cfg, model, system, user, schema=None, timeout=None, temperature=None, debug=False
):
    """Single completion with bounded retries and exponential backoff.

    Returns (text, meta). Raises OllamaError once retries are exhausted.
    """
    num_ctx = size_context(system, user)
    payload = {
        "model": model,
        "prompt": user,
        "system": system,
        "stream": False,
        "options": {
            "temperature": cfg["temperature"] if temperature is None else temperature,
            "num_ctx": num_ctx,
        },
    }
    if schema is not None:
        payload["format"] = schema

    url = normalize_base_url(cfg["base_url"]) + "/api/generate"
    timeout = cfg["timeout_s"] if timeout is None else timeout
    attempts = max(1, int(cfg.get("max_retries", 3)))
    base = float(cfg.get("backoff_base_s", 1.5))
    trace = []
    last = None

    # `timeout` is the budget for this call INCLUDING retries and backoff, not
    # per attempt. Treating it per attempt let three retries overrun the caller's
    # deadline threefold - measured at 532s against a 360s budget.
    call_deadline = time.time() + timeout

    for attempt in range(1, attempts + 1):
        started = time.time()
        remaining = call_deadline - started
        if remaining <= 1:
            raise last or _timeout_error("request")
        try:
            data = _post(url, payload, remaining)
            text = (data.get("response") or "").strip()
            if not text:
                raise OllamaError(
                    "malformed",
                    "Model returned an empty completion.",
                    "The prompt may exceed the model's context window; try fewer files.",
                )
            meta = {
                "attempts": attempt,
                "elapsed_s": round(time.time() - started, 2),
                "eval_count": data.get("eval_count"),
                "num_ctx": num_ctx,
                "done_reason": data.get("done_reason"),
                "output_truncated": data.get("done_reason") == "length",
            }
            # If the server saw fewer prompt tokens than we sent, it silently
            # dropped input and the review covers only part of the code.
            sent = data.get("prompt_eval_count")
            if isinstance(sent, int) and sent >= num_ctx - RESERVE_TOKENS:
                meta["input_possibly_truncated"] = (
                    "prompt used %d of %d context tokens; some input may have been "
                    "dropped by the server" % (sent, num_ctx)
                )
            if debug:
                meta["trace"] = trace
            return text, meta
        except OllamaError as e:
            last = e
            trace.append({"attempt": attempt, "error": e.to_dict()})
            if not e.retryable or attempt == attempts:
                if debug:
                    e.detail = "%s | trace=%s" % (e.detail, json.dumps(trace))
                raise
            # Never sleep past the caller's deadline, and do not start an
            # attempt there is no time left to finish.
            nap = min(base**attempt, max(0.0, call_deadline - time.time()))
            if nap <= 0 or call_deadline - time.time() <= 1:
                if debug:
                    e.detail = "%s | trace=%s" % (e.detail, json.dumps(trace))
                raise
            time.sleep(nap)

    raise last
