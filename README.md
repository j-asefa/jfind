# jfind — semantic find for Unix pipelines

Find files by what they contain, using Jev. Print the matching paths.

```sh
jfind 'Handles authentication' ./src
find ./src -type f -name '*.py' -print0 | jfind -0 'Handles authentication'
git ls-files -z | jfind -0 'Handles authentication' | xargs -0 wc -l
```

One executable Python file. Python 3.10 or newer, no third-party packages.

## Install

Copy `jfind` into a directory on your PATH:

```sh
chmod +x jfind
mkdir -p "$HOME/.local/bin"
cp jfind "$HOME/.local/bin/jfind"
export PATH="$HOME/.local/bin:$PATH"
export typesafe_api_key='YOUR_TYPESAFE_API_KEY'
```

Or run `./jfind 'your query' ./directory` directly. The API key uses the exact
lowercase environment variable shown above. No configuration file or indexing.

Eligible file text is sent to the hosted TypeSafe API over HTTPS. Paths and
filenames stay local. Searches incur normal API usage; nothing is cached.

## Input and output

```text
jfind [-0] [--threshold P] [--max-requests N] QUERY [PATH ...]
```

- Explicit paths select files or recursive directories and take precedence over stdin.
- Without paths, terminal stdin searches `.`; piped/redirected stdin supplies filenames.
  Empty stdin means no files. In scripts, pass `.` to request a directory search.
- Piped directories are skipped. Stdin contains names of files to open, never file contents.
- Path lists use newlines, or NUL with `-0`. A final delimiter is optional. Spaces are preserved.
- `-0` preserves arbitrary Unix filename bytes, including newlines. Use it for `xargs -0`.
- Matches go to stdout immediately, one path per record. Diagnostics go to stderr.
  Piped input retains its order and duplicates; directory scans use filesystem traversal order.
- Newline output to a terminal escapes unusual characters for safe display. Redirected
  output preserves path bytes; newline lists cannot unambiguously represent newline filenames.

Use existing commands for the rest:

```sh
# Restrict extension or modification time before paying for semantic evaluation.
find ./notes -type f -name '*.md' -mtime -30 -print0 |
  jfind -0 'Explains why buying moves the market price'

# Inspect matched content or search it for exact text.
git ls-files -z | jfind -0 'Retries failed requests' | xargs -0 grep -n 'TODO'

# Save a reusable path list.
jfind -0 'Describes market-maker inventory risk' ./research > matches.paths0
```

`--threshold P` is the minimum Jev Noul match probability; default **0.5**,
inclusive. Use `--threshold 0.9` to be more selective. Probabilities are model
judgements, not guarantees. The query is evaluated against file content, not the
filename. A file can still attempt to influence the model; the request treats
its text as untrusted evidence, without claiming immunity to prompt injection.

## Eligible files and limits

This small version reads complete, nonempty UTF-8 text files, removing a UTF-8 BOM
before evaluation. It skips binary/NUL content, invalid UTF-8, hidden paths,
symlinks, directories supplied through stdin, sockets, devices, and FIFOs.

Hidden paths and `node_modules`, `target`, `vendor`, `dist`, `build`, and
`__pycache__` are excluded, including when explicitly supplied. Obvious credential
names (`credentials`, `credentials.json`, `secrets.json/yaml/yml`, `id_rsa`,
`id_dsa`, `id_ecdsa`, `id_ed25519`, and `.pem/.key/.p12/.pfx`) and private-key
content are also skipped. These rules are not a comprehensive secret detector.
This version does **not** parse `.gitignore`; use `git ls-files` to select tracked files.

Files over **24 KiB**, files that change during the read, and unreadable files produce
diagnostics and a failing exit status. They are never silently truncated or counted
as nonmatches. A conservative 32,000-token request check can reject heavily escaped
text even below the file-size limit. Paths are limited to 64 KiB per stdin record.

One file is evaluated at a time, with no index, content cache, temporary snapshots,
or external helper commands. Each evaluation uses at most three HTTP attempts,
a 20-second socket timeout, and finite backoff for transient errors. Server
Retry-After delays are honored up to 30 seconds; longer waits fail without retrying
early. Authentication errors stop immediately. The default total limit is **512
HTTP attempts**, including retries; adjust it with `--max-requests N` or narrow the
input upstream. Matching paths already printed remain valid output if a later
evaluation fails; check the exit status when completeness matters.

| Exit | Meaning |
| --- | --- |
| `0` | Successful scan, including no matches; help/version or closed downstream pipe |
| `1` | File, API, key, input/output, or request-limit failure; results may be partial |
| `2` | Invalid command-line arguments |
| `130` | Ctrl-C |

In shells that support it, `set -o pipefail` preserves failures from `jfind` when
a later command in a pipeline succeeds.

## Verify

Offline tests use synthetic files and a loopback HTTP server. They never use your key:

```sh
python3 -m unittest discover -s tests -v
```

An optional live check uses four included synthetic fixtures, a single query,
and at most six HTTP attempts across all cases, including retries:

```sh
python3 tests/live.py
```

See [VERIFICATION.md](VERIFICATION.md) for actual test results and platform coverage.

## Provider contract

The implementation pins `jev-1.13.0` and uses the Noul primitive at
`POST https://api.typesafe.ai/v1/systemone`. It validates response model, question
identity, probability range, and usage. It neither follows redirects with the key
nor includes remote error bodies in diagnostics.

Official references checked on 2026-09-18:
[TypeSafe API](https://docs.typesafe.ai/api) and
[Jev models and limits](https://docs.typesafe.ai/models).
