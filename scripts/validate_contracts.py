import json
from pathlib import Path

CONTRACT_ROOT = Path(__file__).resolve().parents[1] / "contracts"
REQUIRED_FILES = {
    "task.schema.json",
    "worker-result.schema.json",
    "review-result.schema.json",
    "approval-package.schema.json",
}


def main() -> None:
    found = {path.name for path in CONTRACT_ROOT.glob("*.json")}
    missing = REQUIRED_FILES - found
    if missing:
        raise SystemExit(f"Missing contract files: {sorted(missing)}")

    for path in sorted(CONTRACT_ROOT.glob("*.json")):
        with path.open(encoding="utf-8") as handle:
            document = json.load(handle)
        if document.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            raise SystemExit(f"Unexpected JSON Schema dialect in {path}")
        if document.get("type") != "object":
            raise SystemExit(f"Root schema must be object in {path}")
        print(f"OK {path.relative_to(CONTRACT_ROOT.parent)}")


if __name__ == "__main__":
    main()
