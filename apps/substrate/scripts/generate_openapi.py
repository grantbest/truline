import json
import os
import sys

# main.py is part of the `src` package (it uses relative imports internally),
# so the package's parent -- not src/ itself -- goes on sys.path.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# The app module tree reads these at import (src/database.py raises without
# DATABASE_URL); the OpenAPI document contains none of their values, so
# placeholders let the generator -- and the remedy the in-sync test prints --
# run in a bare shell without truncating openapi.json to nothing first.
for _name, _placeholder in (
    ("DATABASE_URL", "postgresql+asyncpg://openapi:openapi@localhost:5432/openapi"),
    ("SUBSTRATE_API_KEY", "openapi-generator-placeholder"),
    ("SUBSTRATE_ENCRYPTION_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="),
):
    os.environ.setdefault(_name, _placeholder)

from src.main import app  # noqa: E402


def openapi_json() -> str:
    # sort_keys pins the document to a single byte-for-byte representation
    # regardless of set-iteration or hash-seed differences between the
    # process that commits it and the process that checks it in CI.
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    print(openapi_json(), end="")
