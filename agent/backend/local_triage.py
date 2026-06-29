"""Initial local challenge triage for single-challenge mode."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

TEXT_EXTS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".html",
    ".java",
    ".js",
    ".json",
    ".md",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".sage",
    ".sh",
    ".sql",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


@dataclass
class FileTriage:
    path: str
    size: int
    sha256: str
    file_type: str
    strings_sample: str = ""
    text_sample: str = ""


@dataclass
class TriageResult:
    challenge_path: Path
    name: str
    description: str
    metadata: dict[str, Any] = field(default_factory=dict)
    files: list[FileTriage] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    flag_patterns: list[str] = field(default_factory=list)

    def to_prompt(self, limit: int = 80_000) -> str:
        lines: list[str] = [
            "# Initial Local Triage",
            "",
            f"Challenge name: {self.name}",
            f"Challenge path: {self.challenge_path}",
            f"Description: {self.description or 'not provided'}",
            f"Estimated categories: {', '.join(self.categories) or 'unknown'}",
        ]
        if self.flag_patterns:
            lines.append(f"Flag format hints: {', '.join(self.flag_patterns)}")
        if self.metadata:
            lines += ["", "## Metadata", "```json", json.dumps(self.metadata, indent=2)[:12_000], "```"]
        lines += ["", "## Files"]
        for item in self.files:
            lines.append(f"- {item.path} ({item.size} bytes, sha256={item.sha256[:16]}..., type={item.file_type})")
        text_samples: list[str] = []
        for item in self.files:
            sample = item.text_sample or item.strings_sample
            if sample:
                text_samples.append(f"### {item.path}\n```\n{sample[:6000]}\n```")
        if text_samples:
            lines += ["", "## Text/String Samples", *text_samples]
        text = "\n".join(lines)
        if len(text) > limit:
            return text[:limit] + f"\n\n[triage prompt truncated from {len(text)} characters]"
        return text


def run_triage(challenge_path: Path) -> TriageResult:
    challenge_path = challenge_path.resolve()
    metadata = _read_metadata(challenge_path)
    description = _read_description(challenge_path, metadata)
    name = str(metadata.get("name") or challenge_path.name)
    files = [_triage_file(path, challenge_path) for path in _iter_files(challenge_path)]
    categories = _guess_categories(files, description, metadata)
    flag_patterns = _extract_flag_patterns(description)
    return TriageResult(
        challenge_path=challenge_path,
        name=name,
        description=description,
        metadata=metadata,
        files=files,
        categories=categories,
        flag_patterns=flag_patterns,
    )


def _iter_files(root: Path) -> list[Path]:
    ignored_dirs = {".git", "__pycache__", ".venv", "node_modules"}
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignored_dirs]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.is_file():
                out.append(path)
    return sorted(out)


def _read_metadata(root: Path) -> dict[str, Any]:
    for name in ("metadata.yml", "metadata.yaml", "challenge.yml", "challenge.yaml"):
        path = root / name
        if path.exists():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace")) or {}
                return data if isinstance(data, dict) else {"raw": data}
            except Exception as exc:
                return {"metadata_error": str(exc)}
    return {}


def _read_description(root: Path, metadata: dict[str, Any]) -> str:
    parts: list[str] = []
    if metadata.get("description"):
        parts.append(str(metadata["description"]))
    for name in ("description.md", "description.txt", "README.md", "README.txt"):
        path = root / name
        if path.exists():
            parts.append(path.read_text(encoding="utf-8", errors="replace")[:30_000])
    return "\n\n".join(dict.fromkeys(p.strip() for p in parts if p.strip()))


def _triage_file(path: Path, root: Path) -> FileTriage:
    rel = path.relative_to(root).as_posix()
    data = path.read_bytes()
    file_type = _file_type(path)
    text_sample = ""
    strings_sample = ""
    if _looks_text(path, data):
        text_sample = data[:24_000].decode("utf-8", errors="replace")
    elif path.stat().st_size <= 50 * 1024 * 1024:
        strings_sample = _strings(path)
    return FileTriage(
        path=rel,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        file_type=file_type,
        strings_sample=strings_sample,
        text_sample=text_sample,
    )


def _file_type(path: Path) -> str:
    try:
        proc = subprocess.run(
            ["file", "-b", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return (proc.stdout or proc.stderr).strip() or "unknown"
    except Exception as exc:
        return f"unknown ({exc})"


def _strings(path: Path) -> str:
    try:
        proc = subprocess.run(
            ["strings", "-a", "-n", "4", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return "\n".join((proc.stdout or "").splitlines()[:120])
    except Exception:
        return ""


def _looks_text(path: Path, data: bytes) -> bool:
    if path.suffix.lower() in TEXT_EXTS:
        return True
    sample = data[:4096]
    if not sample:
        return True
    non_text = sum(
        1
        for b in sample
        if b == 0 or (b < 9 and b not in (7, 8)) or (9 < b < 13) or (13 < b < 32 and b != 27)
    )
    return non_text / len(sample) < 0.03


def _guess_categories(files: list[FileTriage], description: str, metadata: dict[str, Any]) -> list[str]:
    text = "\n".join(
        [
            description,
            " ".join(str(v) for v in metadata.values()),
            "\n".join(f"{f.path} {f.file_type} {f.text_sample[:2000]} {f.strings_sample[:2000]}" for f in files),
        ]
    ).lower()
    scores = dict.fromkeys(("web", "pwn", "rev", "crypto", "forensics", "misc", "osint", "malware", "ai/ml"), 0)

    def add(category: str, amount: int = 1) -> None:
        scores[category] += amount

    for f in files:
        suffix = Path(f.path).suffix.lower()
        ftype = f.file_type.lower()
        if suffix in {".pcap", ".pcapng", ".evtx", ".raw", ".dd", ".e01", ".zip", ".png", ".jpg", ".jpeg", ".wav", ".mp3", ".pdf"}:
            add("forensics", 3)
        if "elf" in ftype or "pe32" in ftype or suffix in {".exe", ".dll", ".so", ".wasm", ".apk", ".pyc"}:
            add("rev", 3)
        if "not stripped" in ftype or any(k in text for k in ("buffer overflow", "rop", "shellcode", "format string", "libc", "heap")):
            add("pwn", 2)
        if suffix in {".html", ".php", ".js", ".css"} or any(k in f.path.lower() for k in ("app.py", "package.json", "flask", "express", "templates")):
            add("web", 2)
        if suffix in {".onnx", ".pt", ".pth", ".h5", ".safetensors", ".pkl"}:
            add("ai/ml", 3)

    keyword_map = {
        "crypto": ("rsa", "aes", "xor", "cipher", "encrypt", "decrypt", "nonce", "prime", "modulus", "ecc", "lattice", "prng"),
        "web": ("http", "cookie", "jwt", "sqli", "ssti", "xss", "ssrf", "csrf", "upload", "flask", "express", "php"),
        "forensics": ("pcap", "packet", "stego", "metadata", "exif", "memory dump", "disk image", "registry", "wireshark"),
        "osint": ("osint", "geolocation", "social", "username", "where", "who", "identify", "image search"),
        "malware": ("malware", "c2", "beacon", "packed", "obfuscat", "yara", "shellcode"),
        "misc": ("jail", "encoding", "qr", "dns", "game", "esolang", "unicode", "audio"),
        "ai/ml": ("machine learning", "llm", "prompt", "model", "adversarial", "neural", "transformer"),
    }
    for category, keywords in keyword_map.items():
        for keyword in keywords:
            if keyword in text:
                add(category, 2)

    ordered = [cat for cat, score in sorted(scores.items(), key=lambda item: item[1], reverse=True) if score > 0]
    return ordered[:4] or ["misc"]


def _extract_flag_patterns(description: str) -> list[str]:
    patterns = set(re.findall(r"\b[A-Za-z0-9_.:-]{2,40}\{\}", description))
    patterns.update(re.findall(r"\b[A-Za-z0-9_.:-]{2,40}\{[^{}\s]{0,30}\}", description))
    return sorted(patterns)
