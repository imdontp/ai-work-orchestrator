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
    constraints: list[str] = Field(default_factory=list)
    references: list[ContextReference] = Field(default_factory=list)
    prior_artifacts: list[str] = Field(default_factory=list)
    expected_output_schema: str
