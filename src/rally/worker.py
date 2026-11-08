"""Worker implementation for Reset Rally."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from typing import Any, Callable


class AdapterException(Exception):
    """Base exception for CLI adapter results."""


class QuotaExhausted(AdapterException):
    """CLI quota or rate limit reached."""


class AdapterBusy(AdapterException):
    """Model or adapter is temporarily at capacity."""


Busy = AdapterBusy


class AdapterTimeout(AdapterException):
    """Execution timed out."""


class AdapterError(AdapterException):
    """Adapter failed with non-zero exit code."""


class LeaseSet(set):
    """Set of held lease IDs that compares equal to an integer count."""

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, int):
            return len(self) == other
        return super().__eq__(other)


def resolve_family(model_id: str | None) -> str:
    """Resolve model vendor family from model ID string."""
    if not model_id or model_id.lower() in ("brawn", "local"):
        return "brawn"

    mid = model_id.lower()
    if any(k in mid for k in ("claude", "opus", "sonnet", "haiku", "fable")):
        return "anthropic"
    if any(k in mid for k in ("gemini", "argon")):
        return "google"
    if "deepseek" in mid:
        return "deepseek"
    if "qwen" in mid:
        return "qwen"
    if mid.startswith("codex/") or any(k in mid for k in ("gpt-", "sol", "astra", "o1", "o3")):
        return "openai"

    raise ValueError(f"Unknown model family for {model_id}")


def build_argv(cli: str, model: str | None, prompt: str) -> tuple[list[str], str | None]:
    """Build CLI execution command and optional stdin payload."""
    if cli == "claude":
        argv = ["claude", "-p"]
        if model is not None:
            argv.extend(["--model", model])
        argv.append(prompt)
        return argv, None
    if cli == "codex":
        argv = ["codex", "exec"]
        if model is not None:
            argv.extend(["-m", model])
        argv.append(prompt)
        return argv, None
    if cli == "agy":
        argv = ["agy", "-p"]
        if model is not None:
            argv.extend(["--model", model])
        argv.append(prompt)
        return argv, None
    if cli == "opencode":
        argv = ["opencode", "run"]
        if model is not None:
            argv.extend(["-m", model])
        argv.append(prompt)
        return argv, None
    if cli == "brawn":
        return ["brawn", prompt], None

    raise ValueError(f"Unknown CLI adapter: {cli}")


def classify_cli_result(
    cli: str,
    rc: int,
    stdout: str = "",
    stderr: str = "",
    raise_exc: bool = True,
) -> Exception | None:
    """Classify execution exit code and streams into adapter exception types."""
    if rc == 0:
        return None

    combined = f"{stderr}\n{stdout}".lower()
    quota_patterns = (
        "usage limit",
        "limit reached",
        "individual quota reached",
        "go usage limit exceeded",
        "resource_exhausted",
    )
    if any(p in combined for p in quota_patterns):
        exc: Exception = QuotaExhausted(stderr or stdout)
    elif any(p in combined for p in ("model is at capacity", "try again in a moment")):
        exc = AdapterBusy(stderr or stdout)
    elif rc == 124 or stderr.strip().lower() == "timeout":
        exc = AdapterTimeout(stderr or stdout)
    else:
        exc = AdapterError(f"CLI exited with code {rc}: {stderr}")

    if raise_exc:
        raise exc
    return exc


def default_runner(
    argv: list[str],
    stdin: str | None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run CLI subprocess with optional stdin and environment."""
    res = subprocess.run(
        argv,
        input=stdin,
        text=True,
        capture_output=True,
        env=env,
    )
    return res.returncode, res.stdout, res.stderr


def _get_status(obj: Any, default: int = 200) -> int:
    if obj is None:
        return default
    if isinstance(obj, int):
        return obj
    if isinstance(obj, dict):
        return int(obj.get("status", obj.get("status_code", default)))
    if hasattr(obj, "status"):
        return int(obj.status)
    if hasattr(obj, "status_code"):
        return int(obj.status_code)
    if hasattr(obj, "code"):
        return int(obj.code)
    return default


def _get_field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _extract_prompt(lease: Any) -> str:
    if isinstance(lease, dict):
        if "prompt" in lease and isinstance(lease["prompt"], str):
            return lease["prompt"]
        wu = lease.get("wu")
        if isinstance(wu, dict) and "prompt" in wu:
            return str(wu["prompt"])
        if isinstance(wu, str):
            return wu
        return ""
    wu = getattr(lease, "wu", None)
    if wu is not None:
        if isinstance(wu, dict):
            return str(wu.get("prompt", ""))
        return str(getattr(wu, "prompt", wu))
    return str(getattr(lease, "prompt", ""))


class Worker:
    """Participant worker loop for Reset Rally."""

    def __init__(
        self,
        participant: str,
        cli: str,
        model: str | None = None,
        rallies: list[str] | None = None,
        client: Any = None,
        now: Callable[[], float] = time.time,
        runner: Callable[[list[str], str | None, dict[str, str] | None], tuple[int, str, str]] = default_runner,
        window_start: float | None = None,
        window_end: float | None = None,
        buffer_s: float = 600.0,
        est_wu_s: float = 600.0,
        max_tasks: int | None = None,
        env: dict[str, str] | None = None,
        track: str | None = None,
    ) -> None:
        self.participant = participant
        self.cli = cli
        self.model = model
        self.family = resolve_family(model)
        self.track = track or ("brawn" if self.family == "brawn" else "brain")
        self.rallies = list(rallies) if rallies is not None else []
        self.client = client
        self.now = now
        self.runner = runner
        self.window_start = window_start
        self.window_end = window_end
        self.buffer_s = buffer_s
        self.est_wu_s = est_wu_s
        self.max_tasks = max_tasks
        self.env = env if env is not None else dict(os.environ)

        self.state: str = "idle"
        self.submits: int = 0
        self.releases: int = 0
        self.lease_calls: int = 0
        self.held_leases = LeaseSet()
        self.lease_call_times: list[float] = []

    def in_window(self, t: float) -> bool:
        """Check if time t falls within allowable execution window."""
        if self.window_start is not None and t < self.window_start:
            return False
        if self.window_end is not None and (t + self.est_wu_s) > (self.window_end - self.buffer_s):
            return False
        return True

    def lease_calls_before(self, cutoff: float | str) -> int:
        """Return count of lease requests made before cutoff time."""
        if isinstance(cutoff, (int, float)):
            return sum(1 for t in self.lease_call_times if t < cutoff)
        return sum(1 for t in self.lease_call_times if self.window_start and t < self.window_start)

    def step(self) -> bool:
        """Execute one worker cycle: lease, execute CLI, and submit or release."""
        if self.state in ("stopped:quota", "stopped:budget", "quarantined"):
            return False

        if self.max_tasks is not None and self.submits >= self.max_tasks:
            self.state = "stopped:budget"
            return False

        t = self.now()
        if not self.in_window(t):
            return False

        self.lease_calls += 1
        self.lease_call_times.append(t)

        try:
            lease = self.client.lease(
                participant=self.participant,
                family=self.family,
                model=self.model,
                track=self.track,
                rallies=self.rallies,
            )
        except Exception as exc:
            if _get_status(exc) == 403:
                self.state = "quarantined"
                return False
            raise

        if lease is None:
            return False

        status = _get_status(lease)
        if status == 403:
            self.state = "quarantined"
            return False
        if status == 204:
            return False

        lease_id = _get_field(lease, "lease_id") or _get_field(lease, "id")
        if not lease_id:
            return False

        self.held_leases.add(lease_id)
        if self.state == "idle":
            self.state = "active"

        prompt = _extract_prompt(lease)
        argv, stdin = build_argv(self.cli, self.model, prompt)

        # Execute CLI using isolated local env; Coordinator requests never receive credentials.
        try:
            rc, stdout, stderr = self.runner(argv, stdin, self.env)
            classify_cli_result(self.cli, rc, stdout, stderr)
            raw_output = stdout
        except AdapterTimeout:
            if self.cli == "brawn":
                raw_output = json.dumps({"verdict": "timeout"})
            else:
                self.client.release(lease_id, reason="timeout")
                self.held_leases.discard(lease_id)
                self.releases += 1
                return True
        except QuotaExhausted:
            self.client.release(lease_id, reason="rate_limited")
            self.held_leases.discard(lease_id)
            self.releases += 1
            self.state = "stopped:quota"
            return False
        except AdapterBusy:
            self.client.release(lease_id, reason="busy")
            self.held_leases.discard(lease_id)
            self.releases += 1
            return False
        except AdapterError:
            self.client.release(lease_id, reason="adapter_error")
            self.held_leases.discard(lease_id)
            self.releases += 1
            return True
        except Exception:
            self.client.release(lease_id, reason="adapter_error")
            self.held_leases.discard(lease_id)
            self.releases += 1
            return True

        self.held_leases.discard(lease_id)

        try:
            try:
                resp = self.client.submit(lease_id, raw_output=raw_output, model=self.model)
            except TypeError:
                resp = self.client.submit(lease_id, output=raw_output, model=self.model)
            sub_status = _get_status(resp)
        except Exception as exc:
            sub_status = _get_status(exc)
            if sub_status is None:
                raise

        if sub_status == 200:
            self.submits += 1
            if self.max_tasks is not None and self.submits >= self.max_tasks:
                self.state = "stopped:budget"
            return True
        if sub_status == 403:
            self.state = "quarantined"
            return False
        # Status 409 or 422 drops lease and lets worker proceed to next task.
        return True

    def run(self, poll_s: float = 0.0) -> None:
        """Run step loop until state halts or step returns False."""
        while self.step():
            if poll_s > 0:
                time.sleep(poll_s)


def main() -> None:
    """CLI entry point for rally-worker."""
    parser = argparse.ArgumentParser(description="Reset Rally worker")
    parser.add_argument("--participant", default="local")
    parser.add_argument("--cli", default="brawn")
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-tasks", type=int, default=None)
    args = parser.parse_args()
    worker = Worker(
        participant=args.participant,
        cli=args.cli,
        model=args.model,
        max_tasks=args.max_tasks,
    )
    worker.run()


if __name__ == "__main__":
    main()
