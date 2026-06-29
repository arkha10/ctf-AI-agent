# Local Solve Guide

This project solves one CTF challenge folder at a time. It does not poll CTFd, submit flags to CTFd, run a competition coordinator, or require provider API keys.

## Command Reference

```bash
uv run ctf-local-solve CHALLENGE_DIR [options]
```

Options:

- `--skills-path PATH`: path to `ctf-skills-main`; default is detected from `../ctf-skills-main`
- `--workspace PATH`: workspace directory; default `./workspaces/<challenge_name>`
- `--remote HOST:PORT`: live target for web/pwn/remote-service challenges
- `--agents 1|2`: number of Codex solvers; default `2`
- `--max-steps N`: max tool calls per solver; default `500`
- `--max-runtime-minutes N`: optional wall-clock limit
- `--model MODEL`: Codex model id, for example `gpt-5.5`
- `-v`: verbose progress and tool logging

## Examples

Local only:

```bash
uv run ctf-local-solve ./challenges/chal1 \
  --agents 1 \
  --model gpt-5.5 \
  -v
```

Remote service:

```bash
uv run ctf-local-solve ./challenges/chal1 \
  --remote 154.57.164.82:30769 \
  --agents 1 \
  --model gpt-5.5 \
  -v
```

Resume an existing workspace:

```bash
uv run ctf-local-solve ./challenges/chal1 \
  --workspace ./workspaces/chal1 \
  --agents 1 \
  --model gpt-5.5 \
  -v
```

## What The Agent Does

1. Checks `codex --version`.
2. Reads the challenge folder.
3. Runs triage: file list, type, hash, strings, metadata, and likely category.
4. Selects relevant skills from `ctf-skills-main`.
5. Starts one or two Codex solver threads.
6. Gives solvers local tools: bash, read/write file, list files, image view, shared memory, candidate flag recording, final flag reporting.
7. Requires solvers to write concise Indonesian evidence.
8. Requires a runnable solve script unless the challenge is genuinely manual.
9. Generates `reports/writeup.md` after a verified flag, or `reports/run_summary.md` if no flag is verified.

## Writeup Style

Writeups are intentionally short and in Bahasa Indonesia. They include only:

- challenge information
- files provided
- key evidence and solve steps
- solve script content/path
- final flag evidence
- final flag

Long raw triage output stays in `artifacts/triage.md`, not in the final writeup.

## Solve Script Requirement

For web, pwn, and other network challenges, scripts should keep target values near the top:

```python
import os

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "31337"))
BASE_URL = os.environ.get("BASE_URL", f"http://{HOST}:{PORT}")
```

Then users can run:

```bash
HOST=example.com PORT=31337 python3 solve.py
```

For pwn scripts using pwntools, the same pattern applies:

```python
from pwn import *
import os

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "31337"))

io = remote(HOST, PORT)
```

## Safety Model

The agent is permissive because it is intended for a dedicated CTF VM/WSL environment. It can run local commands and install tools. Normal file tools are scoped to:

- the challenge folder
- the workspace folder
- the temporary run folder

Install commands are logged to `artifacts/install_log.md`. Docker commands are logged to `artifacts/docker_log.md`.

Docker is only for challenge services or isolated binaries. The agent itself runs on the host.
