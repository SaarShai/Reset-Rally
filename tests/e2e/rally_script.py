"""Flagship 5-participant mock rally simulation harness and phase execution."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import random
import sys
import threading
from zoneinfo import ZoneInfo

from rally.checks import load_manifest
from rally.server import (
    ClientLog,
    Coordinator,
    DirectClient,
    ResponseWrapper,
    make_server,
)
from rally.worker import Worker, default_runner

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "rally"
SHIMS_DIR = Path(__file__).resolve().parents[1] / "shims"

# Simulated start timestamp: Sunday 2026-11-08 17:30 America/Los_Angeles
SIM_START_DT = dt.datetime(2026, 11, 8, 17, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
SIM_START = SIM_START_DT.timestamp()

PHASES = [
    # (name, start_time_str, end_time_str, online_spec, raw_actions)
    ("P0", "17:30", "17:55", {"alice": ["B", "C"], "dave": ["A", "B"]}, []),
    ("P1", "18:00", "18:10", {"alice": ["B", "C"], "bob": ["B", "C"], "dave": ["A", "B"]},
     [("raw_lease", "eve", ["A"])]),
    ("P2a", "18:15", "18:15", {"eve": ["C", "A"], "dave": ["A", "B"]},
     [("raw_submit_stale", "eve")]),
    ("P2b", "18:20", "18:30", {"carol": ["C", "A"], "dave": ["A", "B"]}, []),
    ("P3", "18:35", "18:50", {"bob": ["A", "B"], "dave": ["A", "B"]}, []),
    ("P4", "18:55", "20:00", "all", []),
]

CANARY_SCHEDULE = {
    ("bob", "rally-c"): {2: "K1"},
    ("bob", "C"): {2: "K1"},
    ("carol", "rally-c"): {1: "K2"},
    ("carol", "C"): {1: "K2"},
    ("dave", "rally-a"): {1: "KA"},
    ("dave", "A"): {1: "KA"},
    ("eve", "rally-c"): {3: "K1"},
    ("eve", "C"): {3: "K1"},
}


def _sim_time_from_str(clock_str: str) -> float:
    parts = clock_str.split(":")
    hour, minute = int(parts[0]), int(parts[1])
    d = dt.datetime(2026, 11, 8, hour, minute, tzinfo=ZoneInfo("America/Los_Angeles"))
    return d.timestamp()


def make_canary_picker():
    def picker(participant: str, rally: str, seq: int) -> str | None:
        p_norm = rally.lower().replace("rally-", "").upper()
        sched = (
            CANARY_SCHEDULE.get((participant, rally))
            or CANARY_SCHEDULE.get((participant, p_norm))
            or CANARY_SCHEDULE.get((participant, f"rally-{p_norm.lower()}"))
        )
        if sched and seq in sched:
            return sched[seq]
        return None

    return picker


class VoteObj:
    def __init__(self, participant: str, family: str, output: Any, status: str, points: int) -> None:
        self.participant = participant
        self.family = family
        self.output = output
        self.status = status
        self.points = points


class LeaseObj:
    def __init__(self, participant: str, state: str) -> None:
        self.participant = participant
        self.state = state


class WuWrapper:
    """Wrapper exposing votes, tiebreaker, value, and lease history for a WU."""

    def __init__(self, wu_id: str, coord: Coordinator) -> None:
        self.id = wu_id
        conn = coord._get_conn()
        self.row = conn.execute("SELECT * FROM work_units WHERE id = ?", (wu_id,)).fetchone()

        sub_rows = conn.execute(
            """
            SELECT s.*, p.name as pname
            FROM submissions s JOIN participants p ON p.id = s.participant_id
            WHERE s.wu_id = ?
            ORDER BY s.id ASC
            """,
            (wu_id,),
        ).fetchall()

        self.votes: list[VoteObj] = []
        for r in sub_rows:
            try:
                out = json.loads(r["output"])
            except Exception:
                out = r["output"]
            self.votes.append(VoteObj(r["pname"], r["family"], out, r["status"], r["points"]))

        # Tiebreaker submission if any
        self.tiebreaker: VoteObj | None = None
        if len(self.votes) >= 3:
            self.tiebreaker = self.votes[-1]

        # Committed value
        self.value: Any = None
        if self.row and self.row["result"]:
            try:
                res = json.loads(self.row["result"])
                self.value = res.get("supply_v", res.get("expr", res.get("dt")))
            except Exception:
                self.value = self.row["result"]

        # Lease records
        lease_rows = conn.execute(
            """
            SELECT l.*, p.name as pname
            FROM leases l JOIN participants p ON p.id = l.participant_id
            WHERE l.wu_id = ?
            ORDER BY l.issued_at ASC
            """,
            (wu_id,),
        ).fetchall()
        self.leases: list[LeaseObj] = [LeaseObj(lr["pname"], lr["status"]) for lr in lease_rows]

        # Committed by participant
        comm_sub = conn.execute(
            """
            SELECT p.name as pname
            FROM submissions s JOIN participants p ON p.id = s.participant_id
            WHERE s.wu_id = ? AND s.status = 'accepted'
            ORDER BY s.id DESC LIMIT 1
            """,
            (wu_id,),
        ).fetchone()
        self.committed_by: str = comm_sub["pname"] if comm_sub else ""


class RallyResult:
    """End-to-end simulation results and assertion accessors."""

    def __init__(
        self,
        coord: Coordinator,
        workers: dict[str, Worker],
        log: ClientLog,
        raw_responses: dict[Any, Any],
        participant_responses: dict[str, list[ResponseWrapper]],
    ) -> None:
        self.coord = coord
        self.workers = workers
        self.log = log
        self.raw_responses = raw_responses
        self.participant_responses = participant_responses

    def final_state(self) -> dict[str, Any]:
        worker_states = {name: w.state for name, w in self.workers.items()}
        return self.coord.get_final_state(worker_states=worker_states)

    def participant(self, name: str) -> Worker:
        return self.workers[name]

    def wu(self, wu_id: str) -> WuWrapper:
        return WuWrapper(wu_id, self.coord)

    def response(self, participant: str, action: str) -> ResponseWrapper:
        return self.raw_responses.get((participant, action), ResponseWrapper({"status": 500}))

    def responses(self, participant: str) -> list[ResponseWrapper]:
        return self.participant_responses.get(participant, [])

    def canary_stats(self) -> dict[str, int]:
        c = self.coord.counters
        return {
            "spoofed": c["canaries_spoofed"],
            "caught": c["canaries_caught"],
            "honest": c["canaries_honest"],
            "false_fail": c["canaries_false_fails"],
        }

    def points(self) -> dict[str, int]:
        st = self.final_state()
        return {p: info["points"] for p, info in st["participants"].items()}

    def holdout_values(self) -> list[str]:
        return ["7919", "10007", "1230"]

    def payload_log_contains_any(self, tokens: list[str]) -> bool:
        return self.log.payload_log_contains_any(tokens)

    def request_log_text(self) -> str:
        return self.log.request_log_text()

    def event_log_sha256(self) -> str:
        # Canonical event log hash from ClientLog events
        return self.log.event_log_sha256()


def _make_inproc_runner(state_dir: Path):
    from shims.shim import respond

    def inproc_runner(argv: list[str], stdin: str | None, env: dict[str, str] | None = None):
        cli = Path(argv[0]).name
        persona = (env or {}).get("SHIM_PERSONA", "alice")
        st_dir = (env or {}).get("SHIM_STATE", str(state_dir))
        prompt = stdin if stdin is not None else argv[-1]
        rc, out, err = respond(cli, persona, prompt, st_dir)
        return rc, out, err

    return inproc_runner


def run(
    server_or_coord: Any,
    clock: Any,
    seed: int = 0,
    mode: str = "inproc",
    after_tick: Any = None,
    tmp_path: Path | None = None,
) -> RallyResult:
    """Execute the flagship 5-participant mock rally simulation."""
    coord: Coordinator = getattr(server_or_coord, "coordinator", server_or_coord)
    coord.canary_picker = make_canary_picker()

    # Load project manifests
    coord.add_project(FIXTURES_DIR / "a_math_physics.json")
    coord.add_project(FIXTURES_DIR / "b_code_opt.json")
    coord.add_project(FIXTURES_DIR / "c_corpus.json")

    # Register participants
    participants = ["alice", "bob", "carol", "dave", "eve"]
    tokens = {}
    for p in participants:
        res = coord.add_participant(p, role="worker", token=f"tok-{p}")
        tokens[p] = res["token"]

    state_dir = tmp_path or (Path("/tmp") / f"rally_sim_{seed}_{mode}")
    if state_dir.exists():
        import shutil
        shutil.rmtree(state_dir, ignore_errors=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    log = ClientLog()
    raw_responses: dict[Any, Any] = {}
    participant_responses: dict[str, list[ResponseWrapper]] = {p: [] for p in participants}

    # Tracking client wrapper recording participant responses
    class TrackingDirectClient(DirectClient):
        def __init__(self, pname: str):
            super().__init__(coord, participant=pname, token=tokens[pname], log=log)
            self._pname = pname

        def submit(self, lease_id: str, raw_output: Any = None, **kwargs: Any) -> ResponseWrapper:
            resp = super().submit(lease_id, raw_output=raw_output, **kwargs)
            participant_responses[self._pname].append(resp)
            return resp

        def lease(self, **kwargs: Any) -> dict[str, Any] | None:
            resp = super().lease(**kwargs)
            if resp is not None and resp.get("status") == 403:
                participant_responses[self._pname].append(ResponseWrapper(resp))
            return resp

    clients = {p: TrackingDirectClient(p) for p in participants}

    # Runner selection
    if mode == "subprocess":
        runner = default_runner
    else:
        runner = _make_inproc_runner(state_dir)

    # Participant environment with sentinel API credentials
    base_env = dict(os.environ)
    base_env.update(
        {
            "ANTHROPIC_API_KEY": "sk-SENTINEL-7f3a",
            "OPENAI_API_KEY": "sk-SENTINEL-7f3a",
            "GEMINI_API_KEY": "SENTINEL-7f3a",
            "SHIM_STATE": str(state_dir),
            "PATH": f"{SHIMS_DIR / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
        }
    )

    t_1800 = _sim_time_from_str("18:00")
    t_2000 = _sim_time_from_str("20:00")

    # Worker instances
    workers: dict[str, Worker] = {
        "alice": Worker(
            participant="alice",
            cli="claude",
            model="claude/claude-sonnet-5-5",
            rallies=["rally-b", "rally-c"],
            client=clients["alice"],
            now=clock,
            runner=runner,
            window_start=t_1800,
            window_end=t_2000,
            buffer_s=600.0,
            est_wu_s=600.0,
            env={**base_env, "SHIM_PERSONA": "alice"},
        ),
        "bob": Worker(
            participant="bob",
            cli="codex",
            model="codex/gpt-6-astra",
            rallies=["rally-b", "rally-c"],
            client=clients["bob"],
            now=clock,
            runner=runner,
            env={**base_env, "SHIM_PERSONA": "bob"},
        ),
        "carol": Worker(
            participant="carol",
            cli="agy",
            model="agy/gemini-3.8-flash-low",
            rallies=["rally-c", "rally-a"],
            client=clients["carol"],
            now=clock,
            runner=runner,
            env={**base_env, "SHIM_PERSONA": "carol"},
        ),
        "dave": Worker(
            participant="dave",
            cli="brawn",
            model="brawn",
            track="brawn",
            rallies=["rally-a", "rally-b"],
            client=clients["dave"],
            now=clock,
            runner=runner,
            env={**base_env, "SHIM_PERSONA": "dave"},
        ),
        "eve": Worker(
            participant="eve",
            cli="opencode",
            model="opencode/deepseek-v4.1-flash-free",
            rallies=["rally-c", "rally-a"],
            client=clients["eve"],
            now=clock,
            runner=runner,
            env={**base_env, "SHIM_PERSONA": "eve"},
        ),
    }

    raw_leases: dict[str, Any] = {}
    rng = random.Random(seed)
    tick = 0

    for phase_name, start_str, end_str, online_spec, raw_actions in PHASES:
        p_start = _sim_time_from_str(start_str)
        p_end = _sim_time_from_str(end_str)

        # Align clock to phase start
        if clock() < p_start:
            clock.advance(p_start - clock())

        if phase_name == "P3":
            conn = coord._get_conn()
            conn.execute("UPDATE projects SET wave = 1 WHERE id = 'rally-b'")

        # Execute raw actions at phase start
        for act in raw_actions:
            act_type = act[0]
            if act_type == "raw_lease":
                target_p = act[1]
                target_rallies = [f"rally-{r.lower()}" for r in act[2]]
                l_res = clients[target_p].lease(
                    participant=target_p,
                    family=workers[target_p].family,
                    track=workers[target_p].track,
                    rallies=target_rallies,
                )
                raw_leases[target_p] = l_res
            elif act_type == "raw_submit_stale":
                target_p = act[1]
                stale_lid = raw_leases[target_p]["lease_id"]
                stale_res = clients[target_p].submit(
                    stale_lid,
                    raw_output={"expr": "(n*(n+1)//2)**2"},
                )
                raw_responses[(target_p, "submit_stale")] = stale_res

        # Phase tick loop
        while clock() <= p_end:
            tick += 1

            # Update online participants and their target rally preference
            if online_spec == "all":
                active_workers = list(workers.values())
            else:
                active_workers = []
                for p_name, r_list in online_spec.items():
                    w = workers[p_name]
                    w.rallies = [f"rally-{r.lower()}" for r in r_list]
                    active_workers.append(w)

            step_iter = 0
            while True:
                step_iter += 1
                if step_iter > 50:
                    break
                any_stepped = False
                rng.shuffle(active_workers)
                for w in active_workers:
                    if w.step():
                        any_stepped = True
                if not any_stepped:
                    break

            # Advance 5 simulated minutes
            clock.advance(300.0)

            if after_tick:
                after_tick(coord.db_path, log, clock(), tick=tick, phase=phase_name, is_final=False)

            # Quiet check for P4 drain
            if phase_name == "P4":
                conn = coord._get_conn()
                open_cnt = conn.execute("SELECT count(*) as cnt FROM work_units WHERE status = 'open' AND is_canary = 0").fetchone()["cnt"]
                active_leases_cnt = conn.execute("SELECT count(*) as cnt FROM leases WHERE status = 'active'").fetchone()["cnt"]
                if open_cnt == 0 and active_leases_cnt == 0:
                    break

    if after_tick:
        after_tick(coord.db_path, log, clock(), tick=tick, phase="Final", is_final=True)

    return RallyResult(coord, workers, log, raw_responses, participant_responses)


def fresh(tmp_path: Path, seed: int = 0, mode: str = "inproc") -> RallyResult:
    """Create a completely fresh coordinator environment and execute run()."""
    from tests.conftest import FakeClock

    clock = FakeClock(START_TIME if "START_TIME" in globals() else SIM_START)
    db_file = tmp_path / "fresh_rally.db"
    coord = Coordinator(db_file, now=clock)
    return run(coord, clock, seed=seed, mode=mode, tmp_path=tmp_path)


run.fresh = fresh
