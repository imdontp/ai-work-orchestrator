from pydantic import BaseModel, Field


class ContextReference(BaseModel):
    kind: str
    path: str
    description: str
    trusted: bool = False


class ContextPackage(BaseModel):
    schema_version: str = "1.0"
    task_id: str
    objective: str
    acceptance_criteria: list[str] = Field(min_length=1)
    #: Constraints on the task as a whole, from the task contract.
    constraints: list[str] = Field(default_factory=list)
    #: What *this node* may and may not do. Kept apart from `constraints` because
    #: merging them misleads: a reviewer given a read-only rule in the same list as
    #: the task's own constraints read "add a function" next to "do not modify any
    #: file" and reported the task as self-contradictory.
    node_rules: list[str] = Field(default_factory=list)
    references: list[ContextReference] = Field(default_factory=list)
    prior_artifacts: list[str] = Field(default_factory=list)
    expected_output_schema: str
