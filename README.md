# jfind

Find files by meaning. Print matching paths for Unix pipelines.

```sh
jfind 'Verifies digital signatures' ./src
find ./src -type f -name '*.py' -print0 | jfind -0 'Retries failed requests'
git ls-files -z | jfind -0 'Handles authentication' | xargs -0 wc -l
```

## Install

Requires Python 3.10+ and the official [TypeSafe SDK](https://github.com/typesafe-ai/typesafe-sdk-python).
From the cloned repository:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
export PATH="$PWD:$PATH"
export typesafe_api_key='YOUR_TYPESAFE_API_KEY'
```

Keep the environment active, or run `.venv/bin/python jfind` directly.

## Usage

```text
jfind [-0] [--threshold P] [--jobs N] [--max-requests N] [--stats] QUERY [PATH ...]
```

Paths select files or recursive directories. Without paths, `jfind` reads piped
filenames or searches `.` when stdin is a terminal. Empty piped input searches nothing.
Matches go to stdout in input order; diagnostics go to stderr. Use `-0` for NUL-delimited paths.

- `--threshold P`: minimum match probability; default **0.5**.
- `--jobs N`: concurrent evaluations, 1–32; default **4**, with connection reuse.
- `--max-requests N`: total HTTP attempt limit, including retries; default **512**.
- `--stats`: print attempt counts and API-reported token usage to stderr.

Eligible file contents are sent to the hosted TypeSafe API. Each file gets one
semantic evaluation; there is no index or result cache. Reads are limited to
complete UTF-8 text files up to **24 KiB**. Hidden/generated paths, symlinks, binary
files and obvious credentials are excluded. Larger or unreadable files fail the
scan; use `git ls-files` or `find` to narrow inputs. `.gitignore` is not parsed.

Exit codes: **0** success/no matches, **1** incomplete scan or other failure,
**2** invalid arguments, **130** interrupted. Partial matches may already be printed.

## Benchmarks

| Query | Files searched | Time | Estimated cost (USD) |
| --- | ---: | ---: | ---: |
| Verifies digital signatures | 322 | 28.52 s | $0.028816 |
| Retries failed webhook deliveries | 322 | 29.10 s | $0.028830 |
| Persists data in SQLite | 322 | 27.68 s | $0.028830 |

Each query scanned the same eligible tracked text files (up to 24 KiB), using
four workers and no result cache. Time includes CLI startup; all scans completed
without retries. [Measurements](benchmarks/2026-09-18-examples.json).

Costs use reported input tokens at [Jev pricing](https://docs.typesafe.ai/models):
**$0.042 per million input tokens**, with free output (checked 2026-09-18).
Timing and cost vary with file contents, query and API conditions.

## Tests

```sh
python -m unittest discover -s tests -v
```

[Verification details and additional measurements](VERIFICATION.md).
