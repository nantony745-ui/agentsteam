from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, StrictStr, StringConstraints, field_validator, model_validator


def normalize_uuid(value: str) -> str:
    return str(UUID(value))


NonBlankText = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r"\S")]
UuidString = Annotated[NonBlankText, AfterValidator(normalize_uuid)]
Harness = Literal["codex", "opencode", "codebuddy", "deepseek"]


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    REVIEW = "review"
    ACCEPTED = "accepted"
    FAILED = "failed"
    CANCELED = "canceled"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_PERMISSION = "waiting_permission"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    INTERRUPTED = "interrupted"


class ApiInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreate(ApiInput):
    path: NonBlankText
    name: NonBlankText | None = None
    check_argv: list[StrictStr] = Field(default_factory=list)
    team_id: UuidString | None = None
    context_mode: Literal["continue", "new"] = "continue"

    @field_validator("path")
    @classmethod
    def absolute_windows_path(cls, value: str) -> str:
        if not PureWindowsPath(value).is_absolute():
            raise ValueError("path 必须是 Windows 绝对路径")
        return value

    @field_validator("check_argv")
    @classmethod
    def executable_required(cls, value: list[str]) -> list[str]:
        if value and not value[0].strip():
            raise ValueError("check_argv 第一项必须是非空 executable")
        return value


class SessionCreate(ApiInput):
    project_id: UuidString
    harness: Harness | None = None
    employee_id: UuidString | None = None
    leader_session_id: UuidString | None = None

    @model_validator(mode="after")
    def one_identity(self):
        if (self.harness is None) == (self.employee_id is None):
            raise ValueError("必须且只能选择 Harness 或队员")
        return self


class HarnessSettingsInput(ApiInput):
    project_id: UuidString | None = None
    config_values: dict[NonBlankText, NonBlankText] = Field(default_factory=dict)


class HarnessProbeInput(HarnessSettingsInput):
    force: StrictBool = False


class MessageCreate(ApiInput):
    message: NonBlankText
    request_id: UuidString


class TaskCreate(ApiInput):
    harness: Harness | None = None
    employee_id: UuidString | None = None
    context_mode: Literal["continue", "new"] | None = None
    goal: NonBlankText
    acceptance: NonBlankText
    request_id: UuidString
    working_dir: NonBlankText | None = None

    @model_validator(mode="after")
    def one_target(self):
        if (self.harness is None) == (self.employee_id is None):
            raise ValueError("必须且只能选择 Harness 或团队员工")
        if self.harness and self.context_mode is not None:
            raise ValueError("context_mode 仅用于团队员工委派")
        return self

    @field_validator("working_dir")
    @classmethod
    def normalize_working_dir(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("working_dir 必须是绝对目录路径")
        try:
            return str(path.resolve())
        except OSError as error:
            raise ValueError("working_dir 不是有效目录路径") from error


class TaskStatusQuery(ApiInput):
    task_ids: list[UuidString]
    wait_ms: int = Field(default=0, strict=True, ge=0, le=1200000)


class EmptyInput(ApiInput):
    """无业务参数的内部请求；拒绝调用者自报身份。"""


class TaskRework(ApiInput):
    feedback: NonBlankText
    request_id: UuidString
    confirm_old_process_stopped: StrictBool = False


class FeedbackCreate(ApiInput):
    message: NonBlankText


class PermissionAnswer(ApiInput):
    option_id: NonBlankText
