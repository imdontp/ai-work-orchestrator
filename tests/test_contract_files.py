from pathlib import Path
import json


def test_contract_files_are_valid_json() -> None:
    root = Path(__file__).resolve().parents[1] / "contracts"
    paths = sorted(root.glob("*.json"))
    assert len(paths) == 4
    for path in paths:
        with path.open(encoding="utf-8") as handle:
            document = json.load(handle)
        assert document["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert document["type"] == "object"
