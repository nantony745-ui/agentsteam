from pydantic import Field, model_validator

from app.domain.models import ApiInput, NonBlankText, UuidString


class TeamInput(ApiInput):
    name: NonBlankText
    member_ids: list[UuidString] = Field(min_length=1)
    leader_id: UuidString

    @model_validator(mode="after")
    def membership(self):
        if len(set(self.member_ids)) != len(self.member_ids):
            raise ValueError("团队成员不可重复")
        if self.leader_id not in self.member_ids:
            raise ValueError("队长必须属于团队成员")
        return self


class TeamUpdate(TeamInput):
    version: int = Field(strict=True, ge=1)
