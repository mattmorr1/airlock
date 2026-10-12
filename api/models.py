from typing import Annotated, Any

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
    """Without project_id, the project is the one whose registered root contains `cwd`; base defaults to its latest integrated tree."""
    parent_session_id: Name
    request: Annotated[str, Field(min_length=1, max_length=20_000)]
    cwd: Annotated[str | None, Field(min_length=1, max_length=4096)] = None
    project_id: Annotated[str | None, Field(pattern=r"^proj_")] = None
    base_artifact_id: Annotated[str | None, Field(pattern=r"^art_")] = None
    contract: dict[str, Any] | None = None  # session-written plan; validated like the planner's and goes straight to planned


class TaskCreated(Strict):
    task_id: str
    status: TaskState
    job_id: str | None


class Approve(Strict):
    contract_digest: Name


class Approved(Strict):
    task_id: str
    status: TaskState
    contract_digest: str
    constraint_epoch: int


class Rollback(Strict):
    harness_version_id: Annotated[str, Field(pattern=r"^hv_")]
