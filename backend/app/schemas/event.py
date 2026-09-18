from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class WSEvent(BaseModel):
    run_id: Optional[int]
    job_id: Optional[int]
    node: str
    status: str
    message: str = ""
    data: dict = Field(default_factory=dict)
