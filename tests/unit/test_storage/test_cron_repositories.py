"""Tests for cron repositories."""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.storage.facade import Storage
from src.storage.models import CronJobModel, CronRunModel


@pytest.fixture
async def storage():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "cron_test.db"
        store = Storage(f"sqlite:///{db_path}")
        await store.initialize()
        yield store
        await store.close()


@pytest.mark.asyncio
async def test_cron_job_repository_create_list_and_status(storage):
    run_at = datetime.utcnow() + timedelta(minutes=30)
    job = CronJobModel(
        user_id=1001,
        job_type="reminder",
        schedule_type="once",
        payload_text="take milk tea",
        chat_id=2001,
        thread_id=0,
        scope_key="1001:2001:0",
        project_dir="/tmp",
        run_at=run_at,
        next_run_at=run_at,
    )

    created = await storage.cron_jobs.create_job(job)
    assert created.id is not None

    jobs = await storage.cron_jobs.list_jobs(user_id=1001)
    assert len(jobs) == 1
    assert jobs[0].payload_text == "take milk tea"

    changed = await storage.cron_jobs.set_status(
        job_id=created.id,
        user_id=1001,
        status="paused",
        next_run_at=None,
        last_error=None,
    )
    assert changed is True

    paused = await storage.cron_jobs.get_job(created.id, user_id=1001)
    assert paused is not None
    assert paused.status == "paused"


@pytest.mark.asyncio
async def test_cron_run_repository_persists_run_history(storage):
    run_at = datetime.utcnow() + timedelta(hours=1)
    job = await storage.cron_jobs.create_job(
        CronJobModel(
            user_id=1002,
            job_type="ai_prompt",
            schedule_type="cron",
            payload_text="daily summary",
            chat_id=2002,
            thread_id=0,
            scope_key="1002:2002:0",
            project_dir="/tmp",
            cron_expr="0 9 * * *",
            engine="claude",
            next_run_at=run_at,
        )
    )
    assert job.id is not None

    run = CronRunModel(
        job_id=job.id,
        user_id=1002,
        started_at=datetime.utcnow(),
        finished_at=datetime.utcnow(),
        success=True,
        output_preview="done",
    )
    run_id = await storage.cron_runs.create_run(run)
    assert run_id > 0

    history = await storage.cron_runs.list_runs(job_id=job.id)
    assert len(history) == 1
    assert history[0].success is True
    assert history[0].output_preview == "done"
