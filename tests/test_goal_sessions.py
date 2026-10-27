from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from web_memory.goal_repository import GoalRepository
from web_memory.store import Database
from web_memory.task_repository import TaskRepository


SCHEMA = Path(__file__).resolve().parents[1] / "08_web_data" / "schema.sql"


@pytest_asyncio.fixture
async def goal_db(tmp_path):
    db = Database(tmp_path / "goals.sqlite3", SCHEMA)
    await db.init()
    try:
        yield db
    finally:
        await db.close()


def evidence(criterion: str = "output verified") -> dict:
    return {"checks": [{"criterion": criterion, "passed": True, "evidence": {"stdout": "verified"}}]}


async def test_goal_create_only_persists_intent_and_explicit_plan(goal_db):
    goals = GoalRepository(goal_db)
    goal = await goals.create(
        "Verify an output", ["output verified"], workspace="temporary-workspace",
        plan=[{"id": "read", "intent": "Read the existing output"}],
    )
    assert goal["status"] == "active"
    assert goal["terminal"] is False
    assert goal["tasks"] == goal["checkpoints"] == []
    assert goal["plan"][0]["id"] == "read"
    assert await goal_db.fetchall("SELECT * FROM tasks") == []


async def test_checkpoint_idempotency_prevents_duplicate_records_and_rejects_drift(goal_db):
    goals = GoalRepository(goal_db)
    goal = await goals.create("Check a file", ["output verified"])
    first = await goals.checkpoint(goal["id"], "read", {"value": "before"}, next_intent="fix", idempotency_key="call-1")
    repeated = await goals.checkpoint(goal["id"], "read", {"value": "before"}, next_intent="fix", idempotency_key="call-1")
    assert repeated == first
    with pytest.raises(ValueError, match="different checkpoint payload"):
        await goals.checkpoint(goal["id"], "read", {"value": "changed"}, idempotency_key="call-1")
    saved = await goals.get(goal["id"])
    assert len(saved["checkpoints"]) == 1
    assert saved["next_intent"] == "fix"


async def test_concurrent_checkpoint_retry_across_connections_is_atomic(goal_db):
    first = GoalRepository(goal_db)
    goal = await first.create("Concurrent delivery", ["output verified"])
    second_db = Database(goal_db.path, SCHEMA)
    await second_db.connect()
    try:
        second = GoalRepository(second_db)
        a, b = await asyncio.gather(
            first.checkpoint(goal["id"], "read", {"observed": "same"}, idempotency_key="retry"),
            second.checkpoint(goal["id"], "read", {"observed": "same"}, idempotency_key="retry"),
        )
        assert a["id"] == b["id"]
        assert len((await first.get(goal["id"]))["checkpoints"]) == 1
    finally:
        await second_db.close()


@pytest.mark.parametrize("passed", [False, "false", "true", 1, 0, None])
async def test_completion_requires_json_true_not_truthiness(goal_db, passed):
    goals = GoalRepository(goal_db)
    goal = await goals.create("Verify a result", ["output verified"])
    checks = evidence()
    checks["checks"][0]["passed"] = passed
    with pytest.raises(ValueError, match="JSON boolean"):
        await goals.finish(goal["id"], "completed", checks)
    assert (await goals.get(goal["id"]))["status"] == "active"


@pytest.mark.parametrize("invalid", [
    {},
    {"checks": []},
    {"checks": [{"criterion": "other", "passed": True, "evidence": "observed"}]},
    {"checks": [{"criterion": "output verified", "passed": True, "evidence": "  "}]},
    {"checks": [{"criterion": "output verified", "passed": True, "evidence": True}]},
])
async def test_completion_requires_exact_acceptance_coverage_and_observed_evidence(goal_db, invalid):
    goals = GoalRepository(goal_db)
    goal = await goals.create("Verify a result", ["output verified"])
    with pytest.raises(ValueError):
        await goals.finish(goal["id"], "completed", invalid)
    assert (await goals.get(goal["id"]))["terminal"] is False


async def test_completion_rejects_missing_and_duplicate_acceptance_checks(goal_db):
    goals = GoalRepository(goal_db)
    goal = await goals.create("Verify two results", ["output verified", "second output verified"])
    with pytest.raises(ValueError, match="cover every"):
        await goals.finish(goal["id"], "completed", evidence())
    repeated = evidence()
    repeated["checks"] *= 2
    with pytest.raises(ValueError, match="unique"):
        await goals.finish(goal["id"], "completed", repeated)


async def test_error_checkpoint_can_be_repaired_by_explicit_later_observation(goal_db):
    goals = GoalRepository(goal_db)
    goal = await goals.create("Fix then verify", ["output verified"])
    await goals.checkpoint(goal["id"], "verify", {"missing": True}, status="failed", error={"code": "NOT_FOUND"})
    with pytest.raises(ValueError, match="latest step"):
        await goals.finish(goal["id"], "completed", evidence())
    await goals.checkpoint(goal["id"], "verify", {"content": "correct"}, idempotency_key="verify-repaired")
    finished = await goals.finish(goal["id"], "completed", evidence())
    assert finished["terminal"] is True
    assert finished["status"] == "completed"
    assert len(finished["checkpoints"]) == 2
    assert finished["checkpoints"][0]["error"]["code"] == "NOT_FOUND"
    assert await goals.finish(goal["id"], "completed", evidence()) == finished
    with pytest.raises(ValueError, match="terminal"):
        await goals.checkpoint(goal["id"], "another", {"unexpected": True})


async def test_all_linked_workers_must_succeed_before_completion(goal_db):
    goals = GoalRepository(goal_db)
    tasks = TaskRepository(goal_db)
    goal = await goals.create("Run explicit job", ["output verified"])
    task = await tasks.create("python_script", {"code": "print('declared')"})
    await goals.checkpoint(goal["id"], "runner", {"scheduled": task["id"]}, task_id=task["id"])
    with pytest.raises(ValueError, match="Worker tasks must succeed"):
        await goals.finish(goal["id"], "completed", evidence())
    await tasks.finish(task["id"], "failed", result={"return_code": 2}, error="explicit failure")
    with pytest.raises(ValueError, match="Worker tasks must succeed"):
        await goals.finish(goal["id"], "completed", evidence())
    assert (await goals.get(goal["id"]))["tasks"][0]["result"]["return_code"] == 2


async def test_successful_worker_result_and_artifact_references_remain_inspectable(goal_db):
    goals = GoalRepository(goal_db)
    tasks = TaskRepository(goal_db)
    goal = await goals.create("Verify explicit job", ["output verified"])
    task = await tasks.create("python_script", {"code": "print('declared')"})
    await tasks.finish(task["id"], "success", result={"return_code": 0}, output_files=["temporary/stdout.txt"])
    await goals.checkpoint(
        goal["id"], "runner", {"return_code": 0}, task_id=task["id"], artifacts=["temporary/stdout.txt"]
    )
    completed = await goals.finish(goal["id"], "completed", evidence())
    assert completed["tasks"][0]["output_files"] == ["temporary/stdout.txt"]
    assert completed["checkpoints"][0]["artifacts"] == ["temporary/stdout.txt"]


async def test_cancel_preserves_running_truth_and_cooperatively_cancels_only_linked_tasks(goal_db):
    goals = GoalRepository(goal_db)
    tasks = TaskRepository(goal_db)
    goal = await goals.create("Cancellable deterministic jobs", ["output verified"])
    running = await tasks.create("python_script", {"code": "print('one')"})
    await tasks.claim_next()
    queued = await tasks.create("python_script", {"code": "print('two')"})
    unrelated = await tasks.create("python_script", {"code": "print('unrelated')"})
    await goals.checkpoint(goal["id"], "first", {"task": running["id"]}, task_id=running["id"], status="running")
    await goals.checkpoint(goal["id"], "second", {"task": queued["id"]}, task_id=queued["id"], status="queued")
    cancelled = await goals.cancel(goal["id"])
    assert cancelled["status"] == "cancelled"
    assert (await tasks.get(running["id"]))["status"] == "running"
    assert (await tasks.get(running["id"]))["cancel_requested"] is True
    assert (await tasks.get(queued["id"]))["status"] == "cancelled"
    assert (await tasks.get(unrelated["id"]))["status"] == "queued"
    assert (await goals.cancel(goal["id"]))["status"] == "cancelled"
    with pytest.raises(ValueError, match="terminal"):
        await goals.checkpoint(goal["id"], "third", {"not_scheduled": True})


async def test_reopen_keeps_checkpoint_error_and_intent_without_replaying_orphaned_task(goal_db):
    goals = GoalRepository(goal_db)
    tasks = TaskRepository(goal_db)
    goal = await goals.create("Recover a session", ["output verified"])
    task = await tasks.create("python_script", {"code": "raise Exception('must not replay')"})
    await tasks.claim_next()
    before = await goals.checkpoint(
        goal["id"], "execute", {"worker_task": task["id"]}, next_intent="Inspect before deciding a retry",
        task_id=task["id"], status="running", idempotency_key="original-call",
    )
    await goal_db.close()
    await goal_db.init()
    reopened = GoalRepository(goal_db)
    assert await TaskRepository(goal_db).mark_orphaned() == 1
    saved = await reopened.get(goal["id"])
    assert saved["checkpoints"][0]["id"] == before["id"]
    assert saved["next_intent"] == "Inspect before deciding a retry"
    assert saved["tasks"][0]["status"] == "failed"
    assert "Worker restarted" in saved["tasks"][0]["error"]
    assert len(await goal_db.fetchall("SELECT * FROM tasks")) == 1
    assert await TaskRepository(goal_db).claim_next() is None
    assert saved["status"] == "active"


async def test_additive_schema_can_initialize_twice_without_changing_legacy_tasks(goal_db):
    tasks = TaskRepository(goal_db)
    original = await tasks.create("python_script", {"code": "print('legacy')"})
    await goal_db.init()
    await goal_db.init()
    assert (await tasks.get(original["id"]))["payload"] == {"code": "print('legacy')"}
    assert (await tasks.get(original["id"]))["status"] == "queued"


async def test_migrate_legacy_database_without_rewriting_existing_task_data(tmp_path):
    legacy_schema = tmp_path / "legacy.sql"
    legacy_schema.write_text(SCHEMA.read_text(encoding="utf-8").split("-- Additive migration:")[0], encoding="utf-8")
    db = Database(tmp_path / "legacy.sqlite3", legacy_schema)
    await db.init()
    try:
        tasks = TaskRepository(db)
        task = await tasks.create("python_script", {"code": "print('preserve')"})
        legacy_row = await db.fetchone("SELECT * FROM tasks WHERE id=?", (task["id"],))
        db.schema_path = SCHEMA
        await db.init()
        assert await db.fetchone("SELECT * FROM tasks WHERE id=?", (task["id"],)) == legacy_row
        goal = await GoalRepository(db).create("Use additive tables", ["output verified"])
        assert goal["status"] == "active"
    finally:
        await db.close()


async def test_finish_rechecks_concurrent_checkpoint_before_marking_completed(goal_db, monkeypatch):
    goals = GoalRepository(goal_db)
    goal = await goals.create("Concurrent new work", ["output verified"])
    original_get = goals.get
    injected = False

    async def get_and_insert(goal_id):
        nonlocal injected
        snapshot = await original_get(goal_id)
        if not injected:
            injected = True
            await goals.checkpoint(goal_id, "new-worker", {"still_running": True}, status="running")
        return snapshot

    monkeypatch.setattr(goals, "get", get_and_insert)
    with pytest.raises(ValueError, match="changed concurrently"):
        await goals.finish(goal["id"], "completed", evidence())
    assert (await original_get(goal["id"]))["status"] == "active"


async def test_cancel_retry_delivers_requests_interrupted_after_goal_marked_cancelled(goal_db, monkeypatch):
    goals = GoalRepository(goal_db)
    goal = await goals.create("Cancel recovery", ["output verified"])
    task = await goals.tasks.create("python_script", {"code": "print('queued')"})
    await goals.checkpoint(goal["id"], "queue", {}, task_id=task["id"], status="queued")
    original_cancel = TaskRepository.request_cancel

    async def interrupted(_self, _task_id):
        raise RuntimeError("simulated process interruption")

    monkeypatch.setattr(TaskRepository, "request_cancel", interrupted)
    with pytest.raises(RuntimeError, match="interruption"):
        await goals.cancel(goal["id"])
    assert (await goals.get(goal["id"]))["status"] == "cancelled"
    assert (await goals.tasks.get(task["id"]))["status"] == "queued"
    monkeypatch.setattr(TaskRepository, "request_cancel", original_cancel)
    retried = await goals.cancel(goal["id"])
    assert retried["tasks"][0]["status"] == "cancelled"


async def test_checkpoint_does_not_commit_or_rollback_embedded_worker_transaction(goal_db):
    goals = GoalRepository(goal_db)
    tasks = TaskRepository(goal_db)
    goal = await goals.create("Shared database isolation", ["output verified"])
    task = await tasks.create("python_script", {"code": "print('untouched')"})
    await goal_db.begin_immediate()
    await goal_db.execute("UPDATE tasks SET status='running' WHERE id=?", (task["id"],))
    checkpoint = asyncio.create_task(goals.checkpoint(goal["id"], "observation", {"value": "persisted"}))
    try:
        await asyncio.sleep(0.05)
        # A separate WAL reader can inspect status, but its writer must wait
        # for the Worker's reserved lock rather than commit that transaction.
        assert goal_db.connection.in_transaction is True
        assert checkpoint.done() is False
        await goal_db.rollback()
        await checkpoint
        assert (await tasks.get(task["id"]))["status"] == "queued"
        assert len((await goals.get(goal["id"]))["checkpoints"]) == 1
    finally:
        await goal_db.rollback()
        if not checkpoint.done():
            checkpoint.cancel()
        await asyncio.gather(checkpoint, return_exceptions=True)


async def test_invalid_goal_operation_keeps_worker_transaction_intact(goal_db):
    goals = GoalRepository(goal_db)
    tasks = TaskRepository(goal_db)
    goal = await goals.create("Shared rollback isolation", ["output verified"])
    task = await tasks.create("python_script", {"code": "print('untouched')"})
    await goal_db.begin_immediate()
    await goal_db.execute("UPDATE tasks SET status='running' WHERE id=?", (task["id"],))
    try:
        with pytest.raises(ValueError, match="Out of range float"):
            await goals.checkpoint(goal["id"], "invalid", {"nan": float("nan")})
        assert goal_db.connection.in_transaction is True
        assert (await tasks.get(task["id"]))["status"] == "running"
        await goal_db.rollback()
        assert (await tasks.get(task["id"]))["status"] == "queued"
    finally:
        await goal_db.rollback()


async def test_goal_status_does_not_observe_uncommitted_worker_success(goal_db):
    goals = GoalRepository(goal_db)
    tasks = TaskRepository(goal_db)
    goal = await goals.create("Committed truth only", ["output verified"])
    task = await tasks.create("python_script", {"code": "print('pending')"})
    await goals.checkpoint(goal["id"], "scheduled", {}, task_id=task["id"])
    await goal_db.begin_immediate()
    await goal_db.execute("UPDATE tasks SET status='success' WHERE id=?", (task["id"],))
    try:
        saved = await goals.get(goal["id"])
        assert saved["tasks"][0]["status"] == "queued"
        with pytest.raises(ValueError, match="Worker tasks must succeed"):
            await goals.finish(goal["id"], "completed", evidence())
        assert goal_db.connection.in_transaction is True
    finally:
        await goal_db.rollback()


async def prepare_replacement(goal_db, *, old_status="failed", new_status="success", new_result=None):
    goals = GoalRepository(goal_db)
    tasks = TaskRepository(goal_db)
    goal = await goals.create("Repair a failed deterministic attempt", ["output verified"])
    old = await tasks.create("python_script", {"code": "raise RuntimeError('first attempt')"})
    await tasks.finish(old["id"], old_status, result={"return_code": 2})
    await goals.checkpoint(goal["id"], "run", {"observed": "first attempt failed"}, task_id=old["id"], status="failed")
    new = await tasks.create("python_script", {"code": "print('repaired')"})
    await tasks.finish(new["id"], new_status, result={"return_code": 0} if new_result is None else new_result)
    return goals, tasks, goal, old, new


@pytest.mark.parametrize("old_status", ["failed", "cancelled"])
async def test_explicit_successful_attempt_replacement_allows_same_goal_completion_without_erasing_history(goal_db, old_status):
    goals, tasks, goal, old, new = await prepare_replacement(goal_db, old_status=old_status)
    with pytest.raises(ValueError, match="Worker tasks must succeed"):
        await goals.finish(goal["id"], "completed", evidence())
    repaired = await goals.checkpoint(
        goal["id"], "run", {"observed": "repaired output verified"}, idempotency_key="run-repaired",
        task_id=new["id"], supersedes_task_id=old["id"], artifacts=["temporary/repaired-output.txt"],
    )
    repeated = await goals.checkpoint(
        goal["id"], "run", {"observed": "repaired output verified"}, idempotency_key="run-repaired",
        task_id=new["id"], supersedes_task_id=old["id"], artifacts=["temporary/repaired-output.txt"],
    )
    assert repeated["id"] == repaired["id"]
    assert repaired["supersedes_task_id"] == old["id"]
    assert repaired["observation"]["supersedes_task_id"] == old["id"]
    completed = await goals.finish(goal["id"], "completed", evidence())
    assert completed["status"] == "completed"
    assert len(completed["checkpoints"]) == len(completed["tasks"]) == 2
    assert (await tasks.get(old["id"]))["status"] == old_status
    assert (await tasks.get(old["id"]))["cancel_requested"] is False
    assert (await tasks.get(new["id"]))["status"] == "success"
    assert len(await goal_db.fetchall("SELECT * FROM tasks")) == 2


@pytest.mark.parametrize("old_status", ["queued", "running", "success"])
async def test_attempt_replacement_rejects_active_or_successful_old_task(goal_db, old_status):
    goals, _tasks, goal, old, new = await prepare_replacement(goal_db, old_status=old_status)
    with pytest.raises(ValueError, match="failed/cancelled"):
        await goals.checkpoint(
            goal["id"], "run", {}, idempotency_key="retry", task_id=new["id"], supersedes_task_id=old["id"]
        )
    assert len((await goals.get(goal["id"]))["checkpoints"]) == 1


@pytest.mark.parametrize("new_status,new_result", [
    ("queued", {"return_code": 0}), ("running", {"return_code": 0}),
    ("failed", {"return_code": 2}), ("cancelled", {"return_code": 0}),
    ("success", {}), ("success", {"return_code": 2}), ("success", {"return_code": False}),
])
async def test_attempt_replacement_requires_terminal_success_with_actual_zero_return_code(goal_db, new_status, new_result):
    goals, _tasks, goal, old, new = await prepare_replacement(goal_db, new_status=new_status, new_result=new_result)
    with pytest.raises(ValueError, match="real success result"):
        await goals.checkpoint(
            goal["id"], "run", {}, idempotency_key="retry", task_id=new["id"], supersedes_task_id=old["id"]
        )


async def test_attempt_replacement_requires_explicit_parameter_and_matching_step(goal_db):
    goals, _tasks, goal, old, new = await prepare_replacement(goal_db)
    with pytest.raises(ValueError, match="explicit supersedes_task_id"):
        await goals.checkpoint(
            goal["id"], "run", {"supersedes_task_id": old["id"]}, idempotency_key="implicit", task_id=new["id"]
        )
    with pytest.raises(ValueError, match="goal and step_id"):
        await goals.checkpoint(
            goal["id"], "wrong-step", {}, task_id=new["id"], supersedes_task_id=old["id"]
        )
    with pytest.raises(ValueError, match="different new task_id"):
        await goals.checkpoint(goal["id"], "run", {}, task_id=old["id"], supersedes_task_id=old["id"])
    with pytest.raises(ValueError, match="checkpoint must be completed"):
        await goals.checkpoint(
            goal["id"], "run", {}, task_id=new["id"], supersedes_task_id=old["id"], status="running"
        )


async def test_attempt_replacement_rejects_foreign_goal_tasks(goal_db):
    goals, _tasks, goal, old, new = await prepare_replacement(goal_db)
    foreign = await goals.create("Another goal", ["output verified"])
    with pytest.raises(ValueError, match="goal and step_id"):
        await goals.checkpoint(
            foreign["id"], "run", {}, task_id=new["id"], supersedes_task_id=old["id"]
        )
    await goals.checkpoint(foreign["id"], "own-step", {}, task_id=new["id"])
    with pytest.raises(ValueError, match="another goal"):
        await goals.checkpoint(
            goal["id"], "run", {}, task_id=new["id"], supersedes_task_id=old["id"], idempotency_key="foreign"
        )


async def test_attempt_replacement_rejects_cycle_even_if_terminal_task_statuses_are_rewritten(goal_db):
    goals, tasks, goal, old, new = await prepare_replacement(goal_db)
    await goals.checkpoint(
        goal["id"], "run", {}, task_id=new["id"], supersedes_task_id=old["id"], idempotency_key="forward"
    )
    # Defensive test against administrative mutation of historical task truth.
    await tasks.finish(old["id"], "success", result={"return_code": 0})
    await tasks.finish(new["id"], "failed", result={"return_code": 2})
    with pytest.raises(ValueError, match="cycle"):
        await goals.checkpoint(
            goal["id"], "run", {}, task_id=old["id"], supersedes_task_id=new["id"], idempotency_key="backward"
        )


@pytest.mark.parametrize("changed_status", ["failed", "running"])
async def test_finish_revalidates_replacement_task_truth(goal_db, changed_status):
    goals, tasks, goal, old, new = await prepare_replacement(goal_db)
    await goals.checkpoint(
        goal["id"], "run", {}, task_id=new["id"], supersedes_task_id=old["id"], idempotency_key="retry"
    )
    changed = new if changed_status == "failed" else old
    await tasks.finish(changed["id"], changed_status, result={"return_code": 2})
    with pytest.raises(ValueError, match="Worker tasks must succeed"):
        await goals.finish(goal["id"], "completed", evidence())
    assert (await goals.get(goal["id"]))["status"] == "active"


async def test_replacement_insertion_rechecks_task_changed_after_validation(goal_db, monkeypatch):
    goals, tasks, goal, old, new = await prepare_replacement(goal_db)
    original_execute = Database.execute
    injected = False

    async def execute_and_change(db, sql, parameters=()):
        nonlocal injected
        if not injected and sql.startswith("INSERT INTO goal_checkpoints") and "supersedes_task_id" in str(tuple(parameters)):
            injected = True
            await tasks.finish(new["id"], "failed", result={"return_code": 2})
        return await original_execute(db, sql, parameters)

    monkeypatch.setattr(Database, "execute", execute_and_change)
    with pytest.raises(ValueError, match="replacement task status changed"):
        await goals.checkpoint(
            goal["id"], "run", {}, task_id=new["id"], supersedes_task_id=old["id"], idempotency_key="racing"
        )
    assert len((await goals.get(goal["id"]))["checkpoints"]) == 1


async def test_finish_sql_guard_rejects_replacement_failure_after_status_snapshot(goal_db, monkeypatch):
    goals, tasks, goal, old, new = await prepare_replacement(goal_db)
    await goals.checkpoint(
        goal["id"], "run", {}, task_id=new["id"], supersedes_task_id=old["id"], idempotency_key="retry"
    )
    original_get = goals.get
    injected = False

    async def get_and_change(goal_id):
        nonlocal injected
        snapshot = await original_get(goal_id)
        if not injected:
            injected = True
            await tasks.finish(new["id"], "failed", result={"return_code": 2})
        return snapshot

    monkeypatch.setattr(goals, "get", get_and_change)
    with pytest.raises(ValueError, match="changed concurrently"):
        await goals.finish(goal["id"], "completed", evidence())
    assert (await original_get(goal["id"]))["status"] == "active"


async def test_one_replacement_does_not_hide_failed_task_linked_to_another_step(goal_db):
    goals, _tasks, goal, old, new = await prepare_replacement(goal_db)
    await goals.checkpoint(goal["id"], "other-step", {}, task_id=old["id"])
    await goals.checkpoint(
        goal["id"], "run", {}, task_id=new["id"], supersedes_task_id=old["id"], idempotency_key="retry"
    )
    with pytest.raises(ValueError, match="same-step replacement"):
        await goals.finish(goal["id"], "completed", evidence())


async def test_goal_validation_rejects_invalid_inputs_and_missing_links(goal_db):
    goals = GoalRepository(goal_db)
    for goal, criteria in ((" ", ["check"]), ("goal", []), ("goal", ["same", "same"]), ("goal", [1])):
        with pytest.raises(ValueError):
            await goals.create(goal, criteria)
    goal = await goals.create("Valid", ["output verified"])
    with pytest.raises(ValueError, match="Linked task not found"):
        await goals.checkpoint(goal["id"], "missing", {}, task_id="task-missing")
    with pytest.raises(ValueError, match="Goal not found"):
        await goals.checkpoint("goal-missing", "step", {})
    with pytest.raises(ValueError, match="goal_cancel"):
        await goals.finish(goal["id"], "cancelled", {})
    assert await goals.get("goal-missing") is None


@pytest.mark.parametrize("outcome", ["failed", "blocked"])
async def test_noncompletion_terminal_outcome_records_evidence_without_false_success(goal_db, outcome):
    goals = GoalRepository(goal_db)
    goal = await goals.create("External dependency", ["output verified"])
    stopped = await goals.finish(goal["id"], outcome, {"reason": "External condition prevents continuing"})
    assert stopped["terminal"] is True
    assert stopped["status"] == outcome
    assert stopped["finished_at"]
    with pytest.raises(ValueError, match="different terminal"):
        await goals.finish(goal["id"], "completed", evidence())