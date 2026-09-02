# Security Policy

## Reporting a vulnerability

Report privately through GitHub's
[private reporting form](https://github.com/hynix666/ollama-reviewer/security/advisories/new).
It is enabled on this repository and visible only to the maintainer. Please do
not open a public issue for a security problem.

This project publishes no contact email by design, so the form is the only
private channel. Expect an initial response within a week or so; this is a
single-maintainer hobby project, not a staffed product, and you should calibrate
expectations accordingly.

If you are reporting something urgent that affects other people rather than just
this repository, GitHub's [abuse reporting](https://github.com/contact/report-abuse)
reaches GitHub staff directly and does not depend on the maintainer.

## Supported versions

The tip of `main` is the only supported version. There are no releases, tags, or
backports — fixes land on `main` and you update by pulling.

## What this tool actually does with your code

Understanding the risk surface matters more here than a version table, because
the tool's whole job is to take source code and hand it to a language model.

**Your source code is sent to the model.** Whatever you point it at — a diff,
named files, piped input — is placed in a prompt and posted to the configured
Ollama endpoint. By default that is `127.0.0.1:11434`, so the code never leaves
your machine. Two settings change that, and both are worth knowing:

* `OLLAMA_HOST` (or `base_url` in `config.json`) can point anywhere. If it
  points at a remote host, your source is sent to that host in plaintext over
  HTTP unless you have arranged TLS yourself.
* `allow_cloud_models` is `false` by default, and models whose names end in
  `:cloud` are refused while it stays false. Setting it to `true` permits
  inference outside your machine.

**Secrets inside reviewed code go to the model too.** The tool does not scan for
or redact credentials. If you review a file containing an API key, that key
enters the model's context and may be written to your Ollama server's logs.
Review the diff you are about to send, particularly when using `--file` on
config or environment files.

**The tool never writes to your filesystem.** It reads code and prints findings.
There is no flag or configuration in which it edits, deletes, or creates source
files. This is a deliberate design constraint, not an accident of the current
implementation — see [CONTRIBUTING.md](CONTRIBUTING.md).

**No credentials are handled.** The tool authenticates to nothing and stores no
tokens. Ollama itself is unauthenticated by default; anything that can reach its
port can use it, which is a property of your Ollama setup rather than of this
tool.

## Treat reviewer output as untrusted data

This is the risk most worth understanding, and it is not hypothetical.

The tool feeds source code into a model and returns the model's output to a
coding agent. If that source code is untrusted — a dependency you are auditing,
a pull request from a stranger, a repository you just cloned — it may contain
text crafted to manipulate the model into emitting instructions rather than
findings. The output then reaches an agent that is capable of acting.

Four properties of the design limit the blast radius:

1. The tool has no write path, so no finding can directly change a file.
2. Findings never set a failing exit code, so no finding can gate a command.
3. The skill instructs the agent to verify every finding against the real code
   before acting, and to report what it rejected.
4. Findings are rendered as data under headings, not as instructions.

None of that is a guarantee. **Reviewer output is model output derived from
input you may not control — treat it as untrusted text, and never let it drive
an action you have not checked.** If you are reviewing genuinely hostile code,
read the findings yourself rather than letting an agent act on them
unsupervised.

## Dependencies

There are none. The tool uses only the Python standard library, so it has no
package supply chain to compromise and nothing to audit beyond this repository
and your Python installation. Keeping it that way is a hard rule for
contributions.

## What is out of scope

* Vulnerabilities in Ollama itself — report those to
  [the Ollama project](https://github.com/ollama/ollama/security).
* Vulnerabilities in the models. A model producing wrong, offensive, or
  misleading findings is a quality issue, not a security issue; use the
  **Review quality** issue template.
* The absence of authentication between this tool and your local Ollama server.
  Ollama ships unauthenticated; securing that port is a matter for your host
  configuration.
* Anything requiring an attacker to already have write access to your
  `config.json` or the ability to set your environment variables. At that point
  they can run arbitrary code as you regardless.
