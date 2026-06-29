# My CTF Agent

This repository bundles:

- `agent/`: local Codex CLI CTF solver
- `ctf-skills-main/`: skill/playbook library used by the solver

Run the agent from `agent/`:

```bash
cd agent
uv sync
codex login
uv run ctf-local-solve ./challenges/nama_challenge --model gpt-5.5 -v
```

For remote web/pwn services:

```bash
uv run ctf-local-solve ./challenges/nama_challenge \
  --remote HOST:PORT \
  --agents 1 \
  --model gpt-5.5 \
  -v
```

The solver does not require API billing keys or CTFd credentials. See `agent/README.md` and `agent/LOCAL_SOLVE.md`.
