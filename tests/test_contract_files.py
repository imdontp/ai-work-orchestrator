import json
from pathlib import Path

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"

#: Every deliverable in workflows/ now has a published schema. Named rather than
#: counted: a count says nothing about which contract went missing.
EXPECTED = {
    "analysis-result.schema.json",
    "approval-package.schema.json",
    "review-result.schema.json",
    "task.schema.json",
    "verification-result.schema.json",
    "worker-result.schema.json",
}


def test_every_expected_contract_is_present() -> None:
    assert {path.name for path in CONTRACTS.glob("*.json")} == EXPECTED


def test_contract_files_are_valid_json() -> None:
    for path in sorted(CONTRACTS.glob("*.json")):
        with path.open(encoding="utf-8") as handle:
            document = json.load(handle)
        assert document["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert document["type"] == "object"


def test_schema_version_carries_an_explicit_type() -> None:
    """`const` alone is rejected by OpenAI's response_format subset, which the Codex
    adapter derives an output schema from."""
    for path in sorted(CONTRACTS.glob("*.json")):
        version = json.loads(path.read_text(encoding="utf-8"))["properties"].get("schema_version")
        if version is not None:
            assert version.get("type") == "string", path.name
