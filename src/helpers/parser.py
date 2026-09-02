import json

import jsonref



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


import json


def resolve_ref(spec: dict, ref: str) -> dict:
    """Resolve a local JSON pointer like '#/components/schemas/ErrorType'."""
    assert ref.startswith("#/"), f"Only local refs supported, got: {ref}"
    node = spec
    for part in ref.lstrip("#/").split("/"):
        node = node[part]
    return node


def find_refs(node, refs: set) -> None:
    """Recursively collect every unique $ref string found in a JSON structure."""
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str):
            refs.add(node["$ref"])
        for v in node.values():
            find_refs(v, refs)
    elif isinstance(node, list):
        for item in node:
            find_refs(item, refs)


def extract_operation_with_refs(spec: dict, path: str, method: str, op: dict) -> dict:
    """Return the raw operation (refs left intact) plus a de-duplicated
    map of resolved definitions for every $ref it uses -- including refs
    nested inside those definitions themselves."""
    seen: set[str] = set()
    to_process: set[str] = set()
    find_refs(op, to_process)

    definitions = {}
    while to_process:
        ref = to_process.pop()
        if ref in seen:
            continue
        seen.add(ref)
        resolved = resolve_ref(spec, ref)
        definitions[ref] = resolved
        nested = set()
        find_refs(resolved, nested)
        to_process |= (nested - seen)

    return {
        "path": path,
        "method": method.upper(),
        "operation": op,           # still has $ref pointers
        "definitions": definitions,  # each unique referenced schema, resolved once
    }


def ingest_openapi_spec(spec_path: str) -> list[dict]:
    with open(spec_path, encoding="utf-8") as f:
        spec = json.load(f)

    operations = []
    for path, path_item in spec.get("paths", {}).items():
        for method, op in path_item.items():
            if method.lower() not in HTTP_METHODS:
                continue
            # OpenAPI allows common parameters (notably `{id}` path parameters)
            # on the path item. Operation-level parameters override matching
            # path-level `(name, in)` pairs.
            path_params = path_item.get("parameters", [])
            op_params = op.get("parameters", [])
            merged_params = {}
            for parameter in [*path_params, *op_params]:
                resolved_parameter = parameter
                if isinstance(parameter, dict) and isinstance(parameter.get("$ref"), str):
                    ref = parameter["$ref"]
                    if ref.startswith("#/"):
                        resolved_parameter = resolve_ref(spec, ref)
                if (
                    isinstance(resolved_parameter, dict)
                    and "name" in resolved_parameter
                    and "in" in resolved_parameter
                ):
                    merged_params[(resolved_parameter["name"], resolved_parameter["in"])] = parameter
                else:
                    merged_params[("$ref", json.dumps(parameter, sort_keys=True))] = parameter
            effective_op = dict(op)
            if merged_params:
                effective_op["parameters"] = list(merged_params.values())
            operations.append(extract_operation_with_refs(spec, path, method, effective_op))

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
       

    print(json.dumps(ops[0]["operation"]["responses"], indent=2))  # full resolved mission/add operation
