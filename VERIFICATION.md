# Verification — 2026-09-18

The CLI now uses the official `AsyncTypeSafeClient` with one shared HTTPX2
connection pool. It retains four concurrent evaluations by default, ordered
output, bounded outstanding work and a shared HTTP-attempt limit. A request hook
counts every SDK attempt, including retries. The SDK owns connection management,
HTTP retries, backoff and response decoding; jfind checks the requested model,
question identity, probability range and required usage counts. A response hook
caps successful and error bodies at 64 KiB while streaming, before SDK buffering.
Requests ask for identity encoding; unexpected compression is rejected before
decoding. Authentication failures stop without reading the response body.

Validation used **typesafe-sdk 0.7.0**, **HTTPX2 2.13.0 and 2.0.0**, and the declared
SOCKS extra (**socksio 1.0.0**). A fresh environment installed `requirements.txt`
with `httpx2==2.0.0`; dependency checks and the full suite passed. All Python sources
compile and parse with Python 3.10 syntax. This does not establish compatibility
across every supported runtime or operating system. Runtime dependencies are listed
in [requirements.txt](requirements.txt); activate the installed environment first.

## Offline checks

`python -m unittest discover -s tests -v`: **40 tests passed** with each HTTPX2 version.

The suite uses synthetic files, SDK mock transports and loopback HTTP servers;
it never uses a real API key. Coverage includes:

- Recursive and piped paths, NUL framing, unusual filenames, duplicates, thresholds,
  exclusions, symlink-safe reads, changed files and file/context size limits.
  Complete private-key headers are excluded without skipping the detector itself.
- Four overlapping SDK calls, shared pool reuse, ordered output and at most
  `--jobs` outstanding evaluations/results even behind a slow first result.
- Output before stdin EOF, cleanup when a pipe remains open, downstream pipe
  closure, KeyboardInterrupt, cancellation of SDK retry waits and active requests.
- Shared request limits including concurrent retries, authentication failures,
  redacted errors, rejected redirects and strict model/answer/usage checks.
- Reconnection after a server closes an idle connection, recovery after malformed
  responses, and file I/O errors without stalling later results.
- `--stats` usage totals across concurrent evaluations and retries.
- HTTPS proxy authentication on CONNECT without exposing the origin API key.
- SOCKS proxy environment variables with `NO_PROXY` bypass and HTTPS proxy
  precedence, including uppercase and lowercase variable names.
- Streamed 2 MiB valid JSON and error bodies stop after at most 65 KiB with the
  mock's 1 KiB chunks, even without an accurate `Content-Length`. Tests also
  cover the exact 64 KiB boundary, buffered mock responses, compression rejection,
  stream closure, no retries/usage for rejected bodies and subsequent recovery.

Unusual-filename tests skip invalid UTF-8 names where the filesystem does not
support them; the remaining filename and NUL-framing cases still run.

## Live fixture benchmark

The original baseline is `main` at **75d9b89**; the previous custom implementation
is **3a97c3e**. Each executable searched the same four fixtures twice, for eight
evaluations per scan. Four configurations ran three times each in rotated order,
with no result cache. Timings include Python startup and dependency imports.

Query: **Implements user sign-in by verifying a password and creating a session**.
Model: **jev-1.13.0**. Threshold: **0.5**.

| Configuration | Median total | Range |
| --- | ---: | ---: |
| Original sequential CLI, fresh connections | 4.460 s | 4.338–4.697 s |
| Previous custom transport, four workers | 0.964 s | 0.944–1.018 s |
| SDK, one concurrent evaluation | 3.505 s | 3.313–3.536 s |
| SDK, four concurrent evaluations (default) | 1.356 s | 1.328–1.472 s |

All 12 scans exited 0, returned exactly the expected two `session.py` paths in
input order, and produced no error diagnostics: **96 file evaluations**. Each scan
allowed at most 24 HTTP attempts, or **288 attempts maximum** for the comparison.
The six SDK scans reported 48 attempts for 48 evaluations, so none needed retries;
the older executables do not expose attempt statistics. Each SDK scan reported
**3,320 input tokens**
and **160 output tokens**, an estimated **$0.00013944** at the published input
price of $0.042 per million tokens; output tokens are free.

The default SDK configuration was **3.29× faster than the original sequential
baseline**. The previous custom transport was faster on this small workload.
Separate five-run `--version` checks measured median startup at 0.096 s for the
original, 0.108 s for the custom implementation and 0.256 s for the SDK version.
Startup accounts for part of the difference; this is not a service latency or
saturation benchmark. The SDK migration reduces custom networking/retry code.

[Raw SDK fixture results](benchmarks/2026-09-18-sdk-fixtures.json) include SDK
versions, source/fixture hashes, individual timings, usage and the pricing source.
[Earlier custom-transport results](benchmarks/2026-09-18.json) are retained as a
separate historical run.

To reproduce the fixture comparison with saved baseline executables:

```sh
python tests/benchmark.py --baseline /path/to/baseline/jfind \
  --previous /path/to/previous/jfind --output /tmp/jfind-benchmark.json
```

Without the optional executables, the benchmark compares the SDK's one- and
four-worker settings (144 attempts maximum for three rounds). `--rounds 1` runs a
shorter comparison. Keys are inherited through the environment and never included
in arguments or reports.

An earlier trial used an eight-attempt cap for eight files. One SDK scan needed
retries and reached that cap after five successful evaluations, correctly exiting
1 with partial results. The benchmark now gives each scan the CLI's normal retry
headroom. That failed trial is retained in
[the retry observation](benchmarks/2026-09-18-sdk-retry-observation.json) and is not
part of the table above; all 12 scans in the final comparison completed.

## Live semantic smoke check

`python tests/live.py` passed all four synthetic cases in one concurrent CLI scan:
**4 attempts**, **1,660 input tokens**, **80 output tokens**, no retries or errors.
It also passed after the review fixes with the identity-encoding request header,
with the same attempts, usage and probabilities.

| Fixture | Expected | Probability |
| --- | --- | ---: |
| session.py | Match | 0.97 |
| recipe.txt | Nonmatch | 0.01 |
| permissions.py | Nonmatch | 0.02 |
| injection.txt | Nonmatch | 0.05 |

[Raw smoke results](benchmarks/2026-09-18-sdk-smoke.json). This checks the retained
prompt on these examples; it is not a broad accuracy or prompt-injection guarantee.

## Repository cost measurement

The repository benchmark searches all tracked paths using **Implements bounded
concurrency for API requests**, with the normal file eligibility rules:

```sh
python tests/benchmark.py --repo --rounds 1 --output /tmp/jfind-repo-benchmark.json
```

The measured snapshot had **17 tracked paths**, of which **15 were evaluated**.
The hidden `.gitignore` and `tests/test_jfind.py` (which contains a synthetic
private-key header for the exclusion test) were skipped by the usual rules.
The CLI itself was evaluated: a discovered false positive in the old private-key
filter was fixed to recognize complete headers, and a regression test covers it.

| Configuration | Wall time | Attempts | Input tokens | Output tokens | Estimated USD cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| SDK, one concurrent evaluation | 6.338 s | 15 | 26,239 | 300 | $0.001102038 |
| SDK, four concurrent evaluations | 2.023 s | 15 | 26,239 | 300 | $0.001102038 |

Both scans exited 0, with no retries, and matched `README.md`, `VERIFICATION.md`,
`jfind` and `tests/test_concurrency.py` in input order. These are single observations
per configuration; the returned matches are not a curated accuracy test. Cost is
computed from the published $0.042 per million input tokens, with output free.

[Raw repository results](benchmarks/2026-09-18-sdk-repository.json) record SDK
versions, the file manifest/hashes, timings and reported usage. Documentation and
this report were updated after the measured snapshot, so future scans include
different inputs. The cost is for one scan, not both benchmark configurations.

## Larger example scans

Three queries each evaluated the same **322 eligible tracked text files**, using
four concurrent evaluations and the default request limit. Files above 24 KiB
and files excluded by the normal eligibility rules were filtered before timing.
All scans exited 0, completed 322 evaluations in 322 HTTP attempts, and needed no
retries. Wall time includes CLI startup. Estimated USD cost uses API-reported
input tokens at $0.042 per million, with free output.

| Query | Files searched | Time | Estimated cost (USD) |
| --- | ---: | ---: | ---: |
| Verifies digital signatures | 322 | 28.52 s | $0.028816 |
| Retries failed webhook deliveries | 322 | 29.10 s | $0.028830 |
| Persists data in SQLite | 322 | 27.68 s | $0.028830 |

[Recorded measurements](benchmarks/2026-09-18-examples.json) contain only the query,
file count, elapsed time and estimated cost. Repository identifiers, paths, source
contents, matched filenames and machine details are omitted.

## Operational limits and example audit

One SDK client is shared by async tasks. `--jobs` bounds both the connection pool
and outstanding evaluations, including completed results awaiting ordered output.
The producer reads piped paths independently. File reads run in threads; already
running local reads finish during shutdown. Async network requests and retry waits
are cancelled before the shared client closes. In-flight requests may still incur
charges, and `--stats` only sums usage in validated successful responses.

The SDK uses at most three HTTP attempts per evaluation, a 20-second timeout per
network operation and a 30-second retry budget. The latter prevents starting a
retry that would exceed the budget; it is not a hard whole-search deadline.
HTTPX2 supplies proxy handling and TLS verification; redirects remain disabled.

All tracked source files, tests, fixtures, documentation and saved reports were
reviewed for examples tied to unrelated projects. README examples now cover
software authentication, request timeouts, retries and database backups. Fixtures
and test inputs remain synthetic; reports contain relative paths and no API keys
or workstation-specific paths.
