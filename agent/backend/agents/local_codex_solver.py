"""Local Codex solver for one challenge, no CTFd, no API-key provider fallback."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import shutil
from typing import Any

from backend.local_shared import LocalSharedMemory
from backend.local_tools import LocalToolbox, image_tuple_to_content_item
from backend.tools.core import _truncate

logger = logging.getLogger(__name__)

_rpc_counter = itertools.count(1)


LOCAL_TOOLS = [
    {
        "name": "bash",
        "description": (
            "Run a local bash command on the host. CWD is the challenge directory. "
            "Environment variables: CTF_CHALLENGE_DIR, CTF_WORKSPACE, CTF_SHARED, "
            "CTF_ARTIFACTS, CTF_REPORTS, CTF_SOLVER_DIR. Keep file access inside those paths "
            "except for explicit tool installation or Docker service commands."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout_seconds": {"type": "integer", "default": 120},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file from /challenge, /workspace, /shared, /artifacts, /reports, /solver, or the run temp dir.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "write_file",
        "description": "Write a file in the local challenge/workspace scope. Prefer /solver/scripts for scripts and /artifacts for generated data.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_files",
        "description": "List files under a scoped path. Defaults to /challenge.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string", "default": "/challenge"}}},
    },
    {
        "name": "view_image",
        "description": "Load a local image into vision context when the Codex model supports it.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "read_shared_memory",
        "description": "Read shared findings, hypotheses, evidence, dead ends, candidate flags, and final flag state.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "append_finding",
        "description": "Append a finding/hypothesis/evidence/dead_end to shared memory for the other solver.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "kind": {"type": "string", "enum": ["finding", "hypothesis", "evidence", "dead_end"], "default": "finding"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "record_candidate_flag",
        "description": "Record a possible flag with evidence. This does not stop the run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "flag": {"type": "string"},
                "evidence": {"type": "string"},
                "source": {"type": "string", "default": ""},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"], "default": "medium"},
            },
            "required": ["flag", "evidence"],
        },
    },
    {
        "name": "report_final_flag",
        "description": "Verify and finalize the flag locally. Use only when evidence proves it came from the challenge.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "flag": {"type": "string"},
                "evidence": {"type": "string"},
                "source": {"type": "string", "default": ""},
            },
            "required": ["flag", "evidence"],
        },
    },
]


class LocalCodexSolver:
    def __init__(
        self,
        agent_name: str,
        model: str,
        prompt: str,
        toolbox: LocalToolbox,
        memory: LocalSharedMemory,
        max_steps: int,
        stop_event: asyncio.Event,
        verbose: bool = False,
    ) -> None:
        self.agent_name = agent_name
        self.model = model
        self.prompt = prompt
        self.toolbox = toolbox
        self.memory = memory
        self.max_steps = max_steps
        self.stop_event = stop_event
        self.verbose = verbose
        self._proc: asyncio.subprocess.Process | None = None
        self._thread_id: str | None = None
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._turn_done = asyncio.Event()
        self._turn_error: str | None = None
        self._structured_output: dict[str, Any] | None = None
        self._last_message = ""
        self._step_count = 0
        self._shared_cursor = 0
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail: list[str] = []

    @property
    def step_count(self) -> int:
        return self._step_count

    async def start(self) -> None:
        if not shutil.which("codex"):
            raise RuntimeError("Codex CLI not found in PATH. Install it and run `codex login` with your ChatGPT subscription.")
        self._proc = await asyncio.create_subprocess_exec(
            "codex",
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())
        try:
            await self._rpc(
                "initialize",
                {
                    "clientInfo": {"name": "ctf-agent-local", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self._send_notification("initialized", {})
            response = await self._rpc(
                "thread/start",
                {
                    "model": self.model,
                    "personality": "pragmatic",
                    "baseInstructions": self.prompt,
                    "cwd": str(self.toolbox.scope.challenge_dir),
                    "approvalPolicy": "never",
                    "sandbox": "danger-full-access",
                    "dynamicTools": LOCAL_TOOLS,
                },
            )
        except Exception as exc:
            await self.stop()
            raise RuntimeError(
                "Failed to start Codex app-server. Confirm `codex --version` works and run `codex login` "
                f"with your ChatGPT Pro subscription. Original error: {exc}"
            ) from exc
        self._thread_id = response.get("result", {}).get("thread", {}).get("id")
        if not self._thread_id:
            await self.stop()
            raise RuntimeError("Codex app-server did not return a thread id.")
        logger.info("[%s] started Codex thread %s", self.agent_name, self._thread_id)

    async def run(self) -> str | None:
        if not self._proc:
            await self.start()
        assert self._thread_id
        turn = 0
        ignore_existing_final = bool(self.toolbox.scope.remote_target)
        while not self.stop_event.is_set() and self._step_count < self.max_steps:
            turn += 1
            prompt = self._turn_prompt(turn)
            if self.verbose:
                logger.info("[%s] turn %d start (steps=%d/%d)", self.agent_name, turn, self._step_count, self.max_steps)
            result = await self._run_turn(prompt)
            if self.verbose:
                logger.info("[%s] turn %d complete (steps=%d/%d)", self.agent_name, turn, self._step_count, self.max_steps)
            final = self.memory.final_flag()
            if final and (not ignore_existing_final or self._final_has_remote_evidence()):
                logger.info("[%s] final flag detected in shared memory", self.agent_name)
                self.stop_event.set()
                return final
            if result:
                return result
        if self.toolbox.scope.remote_target and not self._final_has_remote_evidence():
            return None
        return self.memory.final_flag()

    def _turn_prompt(self, turn: int) -> str:
        if turn == 1:
            return (
                f"You are {self.agent_name}. Start solving now. First read shared memory, then use tools. "
                "When you learn anything useful, append it to shared memory. When stuck, record a dead_end, "
                "read shared memory again, pivot to another hypothesis or skill, and continue."
            )
        return (
            "Continue solving. Read shared memory first, incorporate the other solver's findings, avoid recorded dead ends, "
            "try a new concrete path, and keep using tools until the flag is proven."
        )

    def _final_has_remote_evidence(self) -> bool:
        remote = self.toolbox.scope.remote_target
        if not remote:
            return False
        path = self.memory.shared_dir / "final_flag_evidence.md"
        if not path.exists():
            return False
        text = path.read_text(encoding="utf-8", errors="replace")
        return _mentions_remote_target(
            text,
            self.toolbox.scope.remote_target,
            self.toolbox.scope.remote_host,
            self.toolbox.scope.remote_port,
        )

    async def _run_turn(self, prompt: str) -> str | None:
        self._turn_done.clear()
        self._turn_error = None
        self._structured_output = None
        await self._rpc(
            "turn/start",
            {
                "threadId": self._thread_id,
                "input": [{"type": "text", "text": prompt}],
                "outputSchema": _solver_output_json_schema(),
            },
        )
        await self._turn_done.wait()
        if self._turn_error:
            lower = self._turn_error.lower()
            if any(token in lower for token in ("not logged in", "login", "authentication", "unauthorized")):
                raise RuntimeError(
                    f"Codex authentication failed for {self.agent_name}. Run `codex login`. Error: {self._turn_error}"
                )
            logger.warning("[%s] Codex turn failed: %s", self.agent_name, self._turn_error)
            return None
        if self._structured_output and self._structured_output.get("type") == "flag_found":
            flag = str(self._structured_output.get("flag") or "").strip()
            method = str(self._structured_output.get("method") or "")
            if flag:
                if self.toolbox.scope.remote_target and not _mentions_remote_target(
                    method,
                    self.toolbox.scope.remote_target,
                    self.toolbox.scope.remote_host,
                    self.toolbox.scope.remote_port,
                ):
                    await self.memory.record_candidate_flag(
                        flag,
                        method,
                        "structured Codex output without remote evidence",
                        self.agent_name,
                        confidence="low",
                    )
                    return None
                await self.memory.finalize_flag(flag, method, "structured Codex output", self.agent_name)
                return self.memory.final_flag()
        return None

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                error = self._codex_process_error("Codex app-server exited before replying.")
                for future in list(self._pending.values()):
                    if not future.done():
                        future.set_exception(RuntimeError(error))
                self._turn_done.set()
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_id = msg.get("id")
            if msg_id is not None and ("result" in msg or "error" in msg):
                future = self._pending.pop(msg_id, None)
                if future and not future.done():
                    if "error" in msg:
                        future.set_exception(RuntimeError(msg["error"]))
                    else:
                        future.set_result(msg)
                continue

            method = msg.get("method", "")
            params = msg.get("params", {})
            if method == "item/tool/call" and msg_id is not None:
                await self._handle_tool_call(msg_id, params)
            elif method == "item/completed":
                item = params.get("item", params)
                if item.get("type") == "agentMessage":
                    text = item.get("text", "")
                    if text:
                        self._last_message = text[:4000]
                        if self.verbose:
                            logger.info("[%s] assistant: %s", self.agent_name, _one_line(text, 1000))
                        if text.lstrip().startswith("{"):
                            try:
                                parsed = json.loads(text)
                            except json.JSONDecodeError:
                                parsed = None
                            if isinstance(parsed, dict):
                                self._structured_output = parsed
            elif method == "turn/completed":
                turn_data = params.get("turn", {})
                if self.verbose:
                    status = turn_data.get("status", "unknown")
                    logger.info("[%s] turn status: %s", self.agent_name, status)
                if turn_data.get("status") == "failed":
                    self._turn_error = _format_turn_error(turn_data.get("error"))
                self._turn_done.set()

    async def _handle_tool_call(self, request_id: int, params: dict[str, Any]) -> None:
        tool = params.get("tool", "")
        args = params.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        self._step_count += 1
        if self.verbose:
            logger.info("[%s] tool #%d %s(%s)", self.agent_name, self._step_count, tool, _format_tool_args(tool, args))
        if self.memory.final_flag() and not self.toolbox.scope.remote_target:
            result: str | tuple[bytes, str] = "A final flag has already been found. Read /shared/final_flag.txt and stop."
        elif self._step_count > self.max_steps:
            result = "Max tool step budget reached for this solver. Summarize your best evidence and stop."
        else:
            result = await self._exec_tool(tool, args)
        if self.verbose:
            logger.info("[%s] tool #%d result: %s", self.agent_name, self._step_count, _summarize_result(result))

        content_items: list[dict[str, str]]
        if isinstance(result, tuple):
            content_items = [image_tuple_to_content_item(result)]
        else:
            text = str(result)
            extra = await self._maybe_shared_update()
            if extra:
                text = f"{text}\n\n---\nShared memory update from the other solver:\n{extra}"
            content_items = [{"type": "inputText", "text": _truncate(text)}]
        await self._respond(request_id, {"contentItems": content_items, "success": True})

    async def _exec_tool(self, tool: str, args: dict[str, Any]) -> str | tuple[bytes, str]:
        match tool:
            case "bash":
                return await self.toolbox.bash(str(args.get("command", "")), int(args.get("timeout_seconds", 120)))
            case "read_file":
                return await self.toolbox.read_file(str(args.get("path", "")))
            case "write_file":
                return await self.toolbox.write_file(str(args.get("path", "")), str(args.get("content", "")))
            case "list_files":
                return await self.toolbox.list_files(str(args.get("path", "/challenge")))
            case "view_image":
                return await self.toolbox.view_image(str(args.get("path", "")))
            case "read_shared_memory":
                self._shared_cursor, _ = await self.memory.check_new_findings(self.agent_name, self._shared_cursor)
                return await self.toolbox.read_shared_memory()
            case "append_finding":
                return await self.toolbox.append_finding(str(args.get("content", "")), str(args.get("kind", "finding")))
            case "record_candidate_flag":
                return await self.toolbox.record_candidate_flag(
                    str(args.get("flag", "")),
                    str(args.get("evidence", "")),
                    str(args.get("source", "")),
                    str(args.get("confidence", "medium")),
                )
            case "report_final_flag":
                return await self.toolbox.report_final_flag(
                    str(args.get("flag", "")),
                    str(args.get("evidence", "")),
                    str(args.get("source", "")),
                )
        return f"Unknown tool: {tool}"

    async def _maybe_shared_update(self) -> str:
        if self._step_count % 3 != 0:
            return ""
        self._shared_cursor, text = await self.memory.check_new_findings(self.agent_name, self._shared_cursor)
        return text

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self._proc and self._proc.stdin
        if self._proc.returncode is not None:
            raise RuntimeError(self._codex_process_error("Codex app-server is not running."))
        msg_id = next(_rpc_counter)
        msg: dict[str, Any] = {"id": msg_id, "method": method}
        if params is not None:
            msg["params"] = params
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        self._proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self._proc.stdin.drain()
        try:
            return await asyncio.wait_for(future, timeout=60)
        finally:
            self._pending.pop(msg_id, None)

    async def _respond(self, request_id: int, result: Any) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write((json.dumps({"id": request_id, "result": result}) + "\n").encode())
        await self._proc.stdin.drain()

    async def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write((json.dumps({"method": method, "params": params}) + "\n").encode())
        await self._proc.stdin.drain()

    async def _stderr_loop(self) -> None:
        assert self._proc and self._proc.stderr
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                self._stderr_tail.append(text)
                self._stderr_tail = self._stderr_tail[-20:]

    def _codex_process_error(self, prefix: str) -> str:
        stderr = "\n".join(self._stderr_tail).strip()
        if stderr:
            return f"{prefix}\nCodex stderr:\n{stderr}"
        return prefix

    async def stop(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None


def _format_turn_error(error: Any) -> str:
    if isinstance(error, dict):
        parts = [str(error.get("message") or "unknown Codex error")]
        for key in ("codexErrorInfo", "additionalDetails"):
            if error.get(key):
                parts.append(str(error[key]))
        return " | ".join(parts)
    return str(error)


def _format_tool_args(tool: str, args: dict[str, Any]) -> str:
    if tool == "bash":
        return _one_line(str(args.get("command", "")), 500)
    if tool in {"read_file", "write_file", "list_files", "view_image"}:
        return str(args.get("path", ""))
    if tool == "append_finding":
        return f"{args.get('kind', 'finding')}: {_one_line(str(args.get('content', '')), 500)}"
    if tool in {"record_candidate_flag", "report_final_flag"}:
        return str(args.get("flag", ""))
    if not args:
        return ""
    return _one_line(json.dumps(args, ensure_ascii=False), 500)


def _summarize_result(result: str | tuple[bytes, str]) -> str:
    if isinstance(result, tuple):
        data, mime_type = result
        return f"image {mime_type}, {len(data)} bytes"
    text = str(result)
    lines = text.splitlines()
    first = _one_line(lines[0], 700) if lines else "(no output)"
    return f"{len(text)} chars, {len(lines)} lines; {first}"


def _one_line(text: str, limit: int) -> str:
    cleaned = text.replace("\x00", "\\0")
    compact = " ".join(cleaned.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + f"... [{len(compact)} chars]"


def _mentions_remote_target(text: str, remote_target: str, remote_host: str, remote_port: str) -> bool:
    needles = {
        remote_target,
        remote_host,
        f":{remote_port}",
        f"TARGET_HOST={remote_host}",
        f"TARGET_PORT={remote_port}",
        f"HOST={remote_host}",
        f"PORT={remote_port}",
        f"CTF_REMOTE={remote_target}",
    }
    return any(needle and needle in text for needle in needles)


def _solver_output_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["flag_found"]},
            "flag": {"type": "string"},
            "method": {"type": "string"},
        },
        "required": ["type", "flag", "method"],
        "additionalProperties": False,
    }


async def check_codex_cli() -> str:
    if not shutil.which("codex"):
        raise RuntimeError("Codex CLI not found in PATH. Install Codex CLI, then run `codex login`.")
    proc = await asyncio.create_subprocess_exec(
        "codex",
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
    if proc.returncode != 0:
        raise RuntimeError((stderr or stdout).decode("utf-8", errors="replace").strip())
    version = stdout.decode("utf-8", errors="replace").strip()
    return version or "codex version unknown"
