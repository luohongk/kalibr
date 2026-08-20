from typing import Literal

from pydantic import BaseModel, ConfigDict


class DatasetInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    state: Literal["ready", "busy", "completed"]
    active_task_id: str | None = None
