import json

import pytest
from pydantic import ValidationError

from app.domain.models import (
    FeedbackCreate,
    MessageCreate,
    PermissionAnswer,
    ProjectCreate,
    RunStatus,
    SessionCreate,
    TaskCreate,
    TaskRework,
    TaskStatus,
)

PROJECT_ID = "11111111-1111-4111-8111-111111111111"
REQUEST_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def test_message_preserves_prompt_and_serializes_uuid_as_string():
    message = "  检查日期解析\n    保留缩进  "
    request = MessageCreate(message=message, request_id=REQUEST_ID.upper())
    assert json.loads(request.model_dump_json()) == {
        "message": message, "request_id": REQUEST_ID, "collaboration_mode": None,
    }
    assert isinstance(request.request_id, str)


@pytest.mark.parametrize("mode", ["default", "plan"])
def test_message_preserves_explicit_collaboration_mode(mode):
    request = MessageCreate(message="规划任务", request_id=REQUEST_ID, collaboration_mode=mode)
    assert json.loads(request.model_dump_json())["collaboration_mode"] == mode


@pytest.mark.parametrize("mode", ["agent-full-access", "unknown", 1])
def test_message_rejects_invalid_collaboration_mode(mode):
    with pytest.raises(ValidationError):
        MessageCreate(message="规划任务", request_id=REQUEST_ID, collaboration_mode=mode)


@pytest.mark.parametrize("message", ["", " ", "\t\r\n", "\u3000", None, 123])
def test_empty_or_non_string_message_is_rejected(message):
    with pytest.raises(ValidationError):
        MessageCreate(message=message, request_id=REQUEST_ID)


@pytest.mark.parametrize("request_id", ["", " ", None, 123, "not-a-uuid", "ses_native_opaque"])
def test_request_id_must_be_a_uuid_string(request_id):
    with pytest.raises(ValidationError):
        MessageCreate(message="检查日期解析", request_id=request_id)


@pytest.mark.parametrize("body", [{}, {"message": "检查日期解析"}, {"request_id": REQUEST_ID}])
def test_message_and_request_id_are_required(body):
    with pytest.raises(ValidationError):
        MessageCreate.model_validate(body)


@pytest.mark.parametrize("harness", ["codex", "opencode", "codebuddy"])
def test_session_uses_platform_project_id_and_supported_harness(harness):
    request = SessionCreate(project_id=PROJECT_ID, harness=harness)
    assert request.project_id == PROJECT_ID
    assert request.harness == harness


@pytest.mark.parametrize("field,value", [
    ("native_id", "ses_native_opaque"),
    ("process_pid", 1234),
    ("parent_id", PROJECT_ID),
    ("status", "accepted"),
])
def test_session_input_cannot_assign_runtime_or_parent_identity(field, value):
    with pytest.raises(ValidationError) as caught:
        SessionCreate.model_validate({"project_id": PROJECT_ID, "harness": "codex", field: value})
    assert any(error["type"] == "extra_forbidden" for error in caught.value.errors())


def test_session_rejects_native_project_id_and_unknown_harness():
    with pytest.raises(ValidationError):
        SessionCreate(project_id="ses_native_opaque", harness="codex")
    with pytest.raises(ValidationError):
        SessionCreate(project_id=PROJECT_ID, harness="unknown-agent")


def test_project_keeps_argv_boundaries_and_does_not_require_existing_directory():
    path = r"D:\model-design-only\nonexistent-repo"
    argv = [r"C:\Program Files\Python\python.exe", "-c", "print('a b')", ""]
    request = ProjectCreate(path=path, name="演示仓库", check_argv=argv)
    assert request.path == path
    assert request.check_argv == argv


@pytest.mark.parametrize("path", ["", ".", r"D:relative", r"\rooted-without-drive"])
def test_project_requires_absolute_windows_path(path):
    with pytest.raises(ValidationError):
        ProjectCreate(path=path, name="演示仓库", check_argv=["python"])


@pytest.mark.parametrize("argv", [[""], [" "], ["python", 123], "python -m unittest"])
def test_project_requires_executable_and_string_argument_array(argv):
    with pytest.raises(ValidationError):
        ProjectCreate(path=r"D:\agentdemo", name="演示仓库", check_argv=argv)


def test_project_can_leave_check_command_unconfigured():
    project = ProjectCreate(path=r"D:\agentdemo", name="演示仓库")
    assert project.check_argv == []
    assert ProjectCreate(path=r"D:\other", name="其他仓库", check_argv=[]).check_argv == []
    project.check_argv.append("python")
    assert ProjectCreate(path=r"D:\new", name="新仓库").check_argv == []


def test_task_and_rework_are_separate_requests():
    task = TaskCreate(harness="opencode", goal="修复日期解析", acceptance="支持闰日", request_id=REQUEST_ID)
    rework = TaskRework(feedback="错误日期应报错", request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    assert rework.request_id != task.request_id
    assert rework.confirm_old_process_stopped is False
    assert TaskRework(**{**rework.model_dump(), "confirm_old_process_stopped": True}).confirm_old_process_stopped


@pytest.mark.parametrize("value", ["true", 1, None])
def test_old_process_confirmation_requires_actual_boolean(value):
    with pytest.raises(ValidationError):
        TaskRework(feedback="再次执行", request_id=REQUEST_ID, confirm_old_process_stopped=value)


@pytest.mark.parametrize("model,body", [
    (TaskCreate, {"harness": "codex", "goal": " ", "acceptance": "支持闰日", "request_id": REQUEST_ID}),
    (TaskCreate, {"harness": "codex", "goal": "修复解析", "acceptance": "", "request_id": REQUEST_ID}),
    (TaskRework, {"feedback": "\n", "request_id": REQUEST_ID}),
    (FeedbackCreate, {"message": None}),
    (PermissionAnswer, {"option_id": ""}),
])
def test_task_feedback_and_permission_text_cannot_be_empty(model, body):
    with pytest.raises(ValidationError):
        model.model_validate(body)


def test_permission_keeps_opaque_reject_option_id():
    request = PermissionAnswer(option_id="cancel")
    assert request.model_dump() == {"option_id": "cancel"}
    assert PermissionAnswer(option_id="opaque option / keep spaces ").option_id.endswith(" ")


def test_delivery_and_execution_states_are_not_interchangeable():
    assert TaskStatus("review") == TaskStatus.REVIEW
    assert RunStatus("completed") == RunStatus.COMPLETED
    with pytest.raises(ValueError):
        RunStatus("review")
    with pytest.raises(ValueError):
        TaskStatus("completed")
    with pytest.raises(ValueError):
        TaskStatus("merged")
    with pytest.raises(ValueError):
        RunStatus("accepted")


def test_input_models_can_generate_api_schemas_without_runtime_services():
    for model in (ProjectCreate, SessionCreate, MessageCreate, TaskCreate,
                  TaskRework, FeedbackCreate, PermissionAnswer):
        schema = model.model_json_schema()
        assert schema["additionalProperties"] is False
        assert schema["required"]
