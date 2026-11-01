"""Testes dos contratos A2A (CT-07, RF-02, RNF-03)."""

import json

import pytest
from pydantic import ValidationError

from src.subagents_bridge import (
    AgentResult,
    AgentTask,
    AgentTaskResult,
    SubagentInfo,
    TaskStatus,
    TokenUsage,
)

TASK_PAYLOAD = {
    "$schema": "https://terminas.dev/schemas/a2a-task.json",
    "task_id": "tsk-42a1b9",
    "parent_task_id": None,
    "assigned_agent": "dev-agent",
    "goal": "Executar testes unitários e gerar relatório de cobertura",
    "context": {
        "project_path": "/home/user/project",
        "relevant_rag_chunks": [{"file": "README.md", "lines": "10-25", "content": "..."}],
    },
    "constraints": {
        "max_steps": 10,
        "timeout_seconds": 120,
        "allowed_skills": ["fs_tools", "shell_runner"],
    },
}

RESULT_PAYLOAD = {
    "$schema": "https://terminas.dev/schemas/a2a-result.json",
    "task_id": "tsk-42a1b9",
    "status": "success",
    "summary": "14 testes executados, 14 passaram. Cobertura: 92%.",
    "artifacts": [{"type": "coverage-report", "path": "coverage/report.json"}],
    "token_usage": {
        "prompt_tokens": 1240,
        "completion_tokens": 195,
        "model": "gemini-2.5-flash",
    },
    "error": None,
}


# ------------------------------------------------------------- AgentTask
def test_task_parses_doc_example_and_roundtrips():
    task = AgentTask.model_validate(TASK_PAYLOAD)
    assert task.constraints.max_steps == 10
    assert task.context.relevant_rag_chunks[0].lines == "10-25"
    assert task.to_wire() == TASK_PAYLOAD
    assert AgentTask.model_validate_json(json.dumps(task.to_wire())) == task


def test_task_defaults():
    task = AgentTask(assigned_agent="dev-agent", goal="fazer algo")
    assert task.task_id.startswith("tsk-") and len(task.task_id) == 12
    assert task.parent_task_id is None
    assert task.constraints.max_steps == 15
    assert task.constraints.timeout_seconds == 120
    assert task.context.relevant_rag_chunks == []
    assert task.to_wire()["$schema"] == "https://terminas.dev/schemas/a2a-task.json"
    assert AgentTask(assigned_agent="a", goal="g").task_id != task.task_id


@pytest.mark.parametrize("missing", ["assigned_agent", "goal"])
def test_task_missing_required_field_rejected(missing):
    payload = {k: v for k, v in TASK_PAYLOAD.items() if k != missing}
    with pytest.raises(ValidationError, match=missing):
        AgentTask.model_validate(payload)


@pytest.mark.parametrize(
    "patch",
    [
        {"extra_field": 1},
        {"goal": "   "},
        {"assigned_agent": ""},
        {"$schema": "https://example.com/outro.json"},
        {"constraints": {"max_steps": 16}},
        {"constraints": {"max_steps": 0}},
        {"constraints": {"timeout_seconds": 0}},
        {"constraints": {"unknown": True}},
        {"context": {"relevant_rag_chunks": [{"file": "a.md", "lines": "abc", "content": ""}]}},
        {"context": {"relevant_rag_chunks": [{"file": "", "lines": "1", "content": ""}]}},
    ],
)
def test_task_invalid_payload_rejected(patch):
    with pytest.raises(ValidationError):
        AgentTask.model_validate({**TASK_PAYLOAD, **patch})


def test_task_invalid_json_rejected():
    with pytest.raises(ValidationError):
        AgentTask.model_validate_json("{não é json")


# ----------------------------------------------------------- AgentResult
def test_result_parses_doc_example_and_roundtrips():
    result = AgentResult.model_validate(RESULT_PAYLOAD)
    assert result.status is TaskStatus.SUCCESS
    assert result.token_usage.total_tokens == 1435
    assert result.to_wire() == RESULT_PAYLOAD
    assert AgentResult.model_validate_json(json.dumps(RESULT_PAYLOAD)) == result


def test_ct07_result_without_task_id_and_status_raises_validation_error():
    payload = {"summary": "ok", "artifacts": []}
    with pytest.raises(ValidationError) as exc:
        AgentTaskResult.model_validate(payload)
    missing = {e["loc"][0] for e in exc.value.errors() if e["type"] == "missing"}
    assert missing == {"task_id", "status"}


def test_agent_task_result_is_alias_of_agent_result():
    assert AgentTaskResult is AgentResult


def test_result_minimal_success():
    result = AgentResult(task_id="t1", status="success")
    assert result.summary == "" and result.artifacts == [] and result.token_usage is None


@pytest.mark.parametrize("status", ["failed", "timeout", "cancelled"])
def test_result_non_success_requires_error(status):
    with pytest.raises(ValidationError, match="exige 'error'"):
        AgentResult(task_id="t1", status=status)
    result = AgentResult(task_id="t1", status=status, error="falhou")
    assert result.status.value == status


def test_result_success_cannot_carry_error():
    with pytest.raises(ValidationError, match="não pode ter 'error'"):
        AgentResult(task_id="t1", status="success", error="boom")


@pytest.mark.parametrize(
    "patch",
    [
        {"status": "desconhecido"},
        {"task_id": ""},
        {"unexpected": 1},
        {"$schema": "https://example.com/x.json"},
        {"token_usage": {"prompt_tokens": -1, "completion_tokens": 0, "model": "m"}},
        {"token_usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        {"artifacts": [{"type": "x"}]},
    ],
)
def test_result_invalid_payload_rejected(patch):
    with pytest.raises(ValidationError):
        AgentResult.model_validate({**RESULT_PAYLOAD, **patch})


def test_token_usage_total():
    assert TokenUsage(prompt_tokens=2, completion_tokens=3, model="m").total_tokens == 5


# ---------------------------------------------------------- SubagentInfo
def test_subagent_info_valid_defaults():
    info = SubagentInfo(name="dev-agent", capabilities=["code"])
    assert info.description == "" and info.recommended_models == [] and info.input_schema is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "Dev Agent", "capabilities": ["code"]},
        {"name": "", "capabilities": ["code"]},
        {"name": "dev-agent", "capabilities": []},
        {"name": "dev-agent", "capabilities": ["code", " "]},
        {"name": "dev-agent", "capabilities": ["code", "code"]},
        {"name": "dev-agent", "capabilities": ["code"], "extra": 1},
    ],
)
def test_subagent_info_invalid_rejected(kwargs):
    with pytest.raises(ValidationError):
        SubagentInfo(**kwargs)
