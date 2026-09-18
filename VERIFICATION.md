# Verification — 2026-09-18

This record covers the minimal Unix path-selection tool: one Python executable
that uses Jev to select paths for downstream Unix commands.

## Offline

`python3 -m unittest discover -s tests -v`: **20 tests passed**.

Coverage includes recursive roots, piped input precedence, empty input, streaming
before EOF, spaces and duplicate paths, NUL-delimited newline/non-UTF-8 filenames,
threshold boundaries, oversized/unreadable/changed files, UTF-8 BOM, binary and
credential exclusions, symlinks/FIFOs, missing credentials, closed downstream
pipes, exact HTTP request/authentication schema, response validation, finite
retries, Retry-After, budget accounting, redirects, error redaction, and request
size limits. Network behavior uses a loopback mock server and synthetic keys.

`python3 -m py_compile jfind tests/live.py`: passed. Python 3.10 syntax was
also checked with `ast.parse(..., feature_version=(3, 10))`.

A copy installed outside the source tree successfully ran help, version, an
empty-input search, and invalid-option handling with no helper programs on PATH,
using an absolute Python interpreter path. The executable itself imports only
Python standard-library modules and never launches subprocesses.

## Live

`python3 tests/live.py` ran the real CLI search logic and real hosted adapter
against only the included synthetic fixtures. The supplied key was kept in the
process environment and was not written to an artifact.

Query: **Implements user sign-in by verifying a password and creating a session**.
Default threshold: **0.5**.

| Fixture | Expected | Observed probability | Printed path? | Elapsed |
| --- | --- | ---: | --- | ---: |
| `session.py` | Match | 0.97 | Yes | 15,778 ms |
| `recipe.txt` | Nonmatch | 0.01 | No | 9,500 ms |
| `permissions.py` | Nonmatch | 0.02 | No | 8,798 ms |
| `injection.txt` | Nonmatch | 0.05 | No | 10,827 ms |

All four CLI invocations exited 0 with empty stderr. Actual response model,
validated by the adapter: **`jev-1.13.0`**. Total: **4 HTTP attempts, 1,660 input
tokens, 80 output tokens**, no retries. The declared maximum was six attempts
and a conservative allowance below 24,000 input tokens. No repeated tuning or
relabeling was used to obtain these results.

This is a small integration check, not an accuracy benchmark or a guarantee of
resistance to prompt injection. Calls were sequential; latency is an observation
from this run, not a performance promise.

## Review and limits

Self-review corrected a broken-pipe exception being caught as a file error,
kept output failures separate from read failures, hardened malformed-response
handling, and checked that credentials and remote error bodies cannot appear in
normal diagnostics. Artifacts were scanned for real-key prefixes before packaging.

Executed on **Linux x86_64, Python 3.12.14**. Python 3.10/3.11 and macOS were not
executed. The tool uses Unix file-descriptor APIs; Windows is not supported.
Parent-directory symlinks are rejected as well as symlink files. No caching,
parallel requests, `.gitignore` parser, ranking, or content-viewing mode is included.
Use Unix commands to select, inspect, and process the output paths.
