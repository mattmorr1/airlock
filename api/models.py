from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from controller.states import TaskState

Name = Annotated[str, Field(min_length=1, max_length=200)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Repository(Strict):
    registered_root_id: Name
    default_branch: Name = "main"


class ProjectCreate(Strict):
    name: Name
    repository: Repository
    baseline_artifact_id: Annotated[str, Field(pattern=r"^art_")]
    harness_version_id: Annotated[str, Field(pattern=r"^hv_")]


class ProjectCreated(Strict):
    project_id: str


class TaskCreate(Strict):
    project_id: Annotated[str, Field(pattern=r"^proj_")]
    parent_session_id: Name
    request: Annotated[str, Field(min_length=1, max_length=20_000)]
    base_artifact_id: Annotated[str, Field(pattern=r"^art_")]


class TaskCreated(Strict):
    task_id: str
    status: TaskState
    job_id: str


class Approve(Strict):
    contract_digest: Name


class Approved(Strict):
    task_id: str
    status: TaskState
    contract_digest: str
    constraint_epoch: int


class Rollback(Strict):
    harness_version_id: Annotated[str, Field(pattern=r"^hv_")]
