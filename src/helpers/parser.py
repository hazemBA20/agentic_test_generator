import json
from xml.etree.ElementPath import ops
from xml.etree.ElementPath import ops
import jsonref

from prance import ResolvingParser
from typing import Any

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}



def load_and_resolve_spec(spec_path: str) -> dict:
    """Load an OpenAPI spec and resolve all $refs, without strict
    OpenAPI schema validation (real-world specs are often slightly
    non-compliant — e.g. custom fields on standard objects — and
    that shouldn't block ingestion)."""
    with open(spec_path, encoding="utf-8") as f:
        spec = json.load(f)

    resolved = jsonref.replace_refs(spec, proxies=False,lazy_load=False)
    # materialize into plain dicts, dropping jsonref proxy objects
    return json.loads(json.dumps(resolved))



def ingest_openapi_spec(spec_path: str) -> list[dict]:
    spec = load_and_resolve_spec(spec_path)
    operations = []

    for path, path_item in spec.get("paths", {}).items():
        for method, op in path_item.items():
            if method.lower() not in HTTP_METHODS:
                continue
            operations.append({"path": path, "method": method.upper(), **op})

    return operations
def pretty_print_operations(operations: list[dict]) -> None:
    """Pretty-print a list of extracted operations to the console."""

    method_colors = {
        "GET": "🟢", "POST": "🟡", "PUT": "🔵",
        "PATCH": "🟣", "DELETE": "🔴",
    }

    print(f"\n{'='*70}")
    print(f"OPERATIONS ({len(operations)} total)")
    print(f"{'='*70}")

    for i, op in enumerate(operations, start=1):
        icon = method_colors.get(op.get("method", ""), "⚪")
        summary = op.get("summary") or "(no summary)"
        print(f"\n[{i}] {icon} {op.get('method'):6} {op.get('path')}")
        print(f"    {summary}")

        if op.get("parameters"):
            print(f"    Parameters: {len(op['parameters'])}")
            for p in op["parameters"]:
                req = "required" if p.get("required") else "optional"
                print(f"      - {p.get('name')} ({p.get('in')}, {req})")

        if op.get("requestBody"):
            print(f"    Has request body: yes")

        if op.get("responses"):
            codes = ", ".join(op["responses"].keys())
            print(f"    Response codes: {codes}")

    print(f"\n{'='*70}\n")

if __name__ == "__main__":
    ops = ingest_openapi_spec("spec.json")
    # pretty_print_operations(ops)
       

    print(json.dumps(ops[3], indent=2))  # full resolved mission/add operation