from pydantic import Field

from app.domain.models import ApiInput, Harness, NonBlankText


class EmployeeInput(ApiInput):
    name: NonBlankText
    role: str = ""
    instructions: str = ""
    responsibilities: str = ""
    principles: str = ""
    growth: str = ""
    harness: Harness | None = None
    config_values: dict[NonBlankText, NonBlankText] = Field(default_factory=dict)


class EmployeeUpdate(EmployeeInput):
    version: int = Field(strict=True, ge=1)
