from __future__ import annotations

import pytest

from a2a.helpers import new_data_part, new_task, new_text_part
from a2a.types import Message, Role, TaskState

from cider_agent import cli
from cider_agent.errors import TextRequestExecutionError


def test_call_local_a2a_returns_completed_task(monkeypatch) -> None:
    async def fake_send_local_a2a_request(message: Message):
        assert message.role == Role.ROLE_USER
        return new_task(
            task_id="task-1",
            context_id="ctx-1",
            state=TaskState.TASK_STATE_COMPLETED,
            artifacts=[
                {
                    "artifact_id": "artifact-1",
                    "name": "cider-agent-result",
                    "parts": [new_data_part({"status": "ok"}, media_type="application/json")],
                    "metadata": {"reasoning": "thinking thoughts"},
                }
            ],
        )

    monkeypatch.setattr(cli, "_send_local_a2a_request", fake_send_local_a2a_request)

    task = cli._call_local_a2a(
        cli._build_user_message(action="pause", parameters={}),
    )

    assert task.status.state == TaskState.TASK_STATE_COMPLETED


def test_call_local_a2a_raises_for_failed_task(monkeypatch) -> None:
    failed = new_task(
        task_id="task-2",
        context_id="ctx-2",
        state=TaskState.TASK_STATE_FAILED,
        history=[],
    )
    failed.status.message.CopyFrom(
        Message(
            role=Role.ROLE_AGENT,
            parts=[
                new_text_part("No active session is running.", media_type="text/plain"),
                new_data_part(
                    {"status": "error", "message": "No active session is running."},
                    media_type="application/json",
                ),
            ],
        )
    )

    async def fake_send_local_a2a_request(message: Message):
        return failed

    monkeypatch.setattr(cli, "_send_local_a2a_request", fake_send_local_a2a_request)

    with pytest.raises(TextRequestExecutionError, match="No active session is running."):
        cli._call_local_a2a(
            cli._build_user_message(action="stop", parameters={}),
        )
