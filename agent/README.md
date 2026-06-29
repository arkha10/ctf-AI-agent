# Local CTF Agent

Local single-challenge CTF solver powered by Codex CLI and a ChatGPT subscription login. It is designed for people who do not use OpenAI/Anthropic/Gemini API billing keys.

The agent runs on your host or WSL machine, reads one challenge folder, selects relevant playbooks from `ctf-skills-main`, starts one or two Codex solver threads, shares findings through file-based memory, and writes a concise Indonesian writeup plus a reproduceable solve script when a flag is found.

## What Is Included

- `ctf-local-solve` and `ctf-solve-local` CLI entrypoints
- local triage for files, hashes, strings, metadata, and likely category
- skill loader for the bundled `../ctf-skills-main` repository
- one or two parallel local Codex solvers
- shared memory in `workspaces/<challenge>/shared`
- permissive local command/file tools scoped to the challenge and workspace
- optional `--remote host:port` support for web/pwn/remote-service challenges
- concise `reports/writeup.md` in Bahasa Indonesia
- solver instruction to create `solve.py` or `solve.sh` with editable `HOST`, `PORT`, and `BASE_URL`

## Requirements

- Python 3.13+
- `uv`
- Codex CLI in `PATH`
- `codex login` already completed with your ChatGPT subscription
- `bubblewrap` on Linux/WSL if Codex app-server requires it
- local CTF tools as needed by each challenge
- Docker only when a challenge itself needs Docker

No `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, CTFd URL, or CTFd token is required.

## Install

From this directory:

```bash
uv sync
codex login
```

If this project is on `/mnt/c` and `uv sync` has permission/link issues:

```bash
export UV_CACHE_DIR=/tmp/uv-cache
export UV_PROJECT_ENVIRONMENT=/tmp/ctf-agent-venv
export UV_LINK_MODE=copy
uv sync
```

## Usage

Basic local solve:

```bash
uv run ctf-local-solve ./challenges/nama_challenge
```

With explicit skills path:

```bash
uv run ctf-local-solve ./challenges/nama_challenge \
  --skills-path ../ctf-skills-main
```

Use one GPT-5.5 solver:

```bash
uv run ctf-local-solve ./challenges/nama_challenge \
  --agents 1 \
  --model gpt-5.5 \
  -v
```

For a public web/pwn service:

```bash
uv run ctf-local-solve ./challenges/nama_challenge \
  --remote 154.57.164.82:30769 \
  --agents 1 \
  --model gpt-5.5 \
  -v
```

When `--remote` is set, the solver must prove the flag from the remote target before finalizing. It also receives these environment variables in bash:

```bash
CTF_REMOTE
CTF_REMOTE_HOST
CTF_REMOTE_PORT
TARGET_HOST
TARGET_PORT
HOST
PORT
BASE_URL
```

## Workspace

Each challenge gets:

```text
workspaces/<challenge>/
├── shared/
│   ├── findings.jsonl
│   ├── hypotheses.md
│   ├── evidence.md
│   ├── dead_ends.md
│   ├── candidate_flags.md
│   ├── final_flag.txt
│   └── final_flag_evidence.md
├── solver_a/
│   ├── notes.md
│   ├── commands.log
│   └── scripts/
├── solver_b/
│   ├── notes.md
│   ├── commands.log
│   └── scripts/
├── artifacts/
│   ├── triage.md
│   ├── selected_skills.md
│   ├── run_config.md
│   ├── install_log.md
│   └── docker_log.md
└── reports/
    ├── writeup.md
    └── run_summary.md
```

Rerunning the same command with the same workspace resumes from prior shared memory.

## Expected Solve Script

For web, pwn, and remote-service challenges, the agent is instructed to create a script with editable variables near the top, for example:

```python
import os

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "31337"))
BASE_URL = os.environ.get("BASE_URL", f"http://{HOST}:{PORT}")
```

Run it against another target by changing environment variables:

```bash
HOST=154.57.164.82 PORT=30769 python3 solve.py
```

## Repository Layout

```text
my-ctf-agent/
├── agent/
│   ├── backend/
│   ├── LOCAL_SOLVE.md
│   ├── README.md
│   └── pyproject.toml
└── ctf-skills-main/
    ├── solve-challenge/
    ├── ctf-web/
    ├── ctf-pwn/
    ├── ctf-reverse/
    ├── ctf-crypto/
    └── ...
```

Upload both `agent/` and `ctf-skills-main/` if you want users to clone one repository and run the agent immediately.
