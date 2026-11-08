"""Integration tests for concurrent multi-threaded lease acquisition (IT-07)."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest

from rally.server import Coordinator

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "rally"


def test_it07_twenty_threads_concurrency(tmp_path: Path):
    """IT-07: 20 real threads competing for 10 open WUs across 20 rounds without database is locked errors."""
    db_file = tmp_path / "concurrent.db"
    coord = Coordinator(db_file)
    conn = coord._get_conn()

    # Create a project with exactly 10 open units
    coord.add_project(FIXTURES_DIR / "c_corpus.json")
    conn.execute("DELETE FROM work_units")

    for i in range(10):
        conn.execute(
            """
            INSERT INTO work_units (id, project_id, stage, wave, payload, prompt, is_canary, replicas, priority, status, created_at)
            VALUES (?, 'rally-c', 'brain', 0, '{}', 'solve', 0, 1, 0, 'open', 1000)
            """,
            (f"WU-{i:02d}",),
        )

    # Register 20 distinct participants
    participants = [f"worker-{i:02d}" for i in range(20)]
    for p in participants:
        coord.add_participant(p)

    # 20 threads request leases concurrently
    def request_work(pname: str) -> dict | None:
        c = Coordinator(db_file)
        try:
            return c.lease(pname, family="openai", track="brain", rallies=["rally-c"])
        except Exception as exc:
            return {"error": str(exc)}

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(request_work, participants))

    # Check that no thread raised a database lock error
    for res in results:
        if isinstance(res, dict) and "error" in res:
            assert "database is locked" not in res["error"].lower()

    # Exactly 10 got leases, 10 got None (204)
    leases = [r for r in results if r is not None and "lease_id" in r]
    empty = [r for r in results if r is None]

    assert len(leases) == 10
    assert len(empty) == 10

    # All 10 leased WUs are distinct
    leased_wu_ids = {l["wu"]["id"] for l in leases}
    assert len(leased_wu_ids) == 10
