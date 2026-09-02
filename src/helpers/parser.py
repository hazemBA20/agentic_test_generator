"""Ingest OpenAPI documents and Postman collections into operation wrappers."""

import json
from pathlib import Path

import yaml

from .postman_parser import ingest_postman_collection


HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}


def _load_document(spec_path: str) -> dict:
    path = Path(spec_path)
    try:
        with path.open(encoding="utf-8") as source:
            document = yaml.safe_load(source)
    except OSError as exc:
        raise ValueError(f"Could not read API source {spec_path!r}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid JSON/YAML in API source {spec_path!r}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(
            f"Invalid API source {spec_path!r}: expected a JSON/YAML object at the document root"
        )
    return document


def resolve_ref(spec: dict, ref: str) -> dict:
    """Resolve an RFC 6901 local JSON pointer such as ``#/components/schemas/Pet``."""
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise ValueError(f"Only local $ref pointers are supported, got {ref!r}")
    node = spec
    try:
        for encoded_part in ref[2:].split("/"):
            part = encoded_part.replace("~1", "/").replace("~0", "~")
            if isinstance(node, list):
                node = node[int(part)]
            else:
                node = node[part]
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise ValueError(f"Could not resolve local $ref {ref!r}") from exc
    if not isinstance(node, dict):
        raise ValueError(f"Local $ref {ref!r} does not point to an object")
    return node


def find_refs(node, refs: set) -> None:
    """Recursively collect every unique ``$ref`` string in a JSON structure."""
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str):
            refs.add(node["$ref"])
        for value in node.values():
            find_refs(value, refs)
    elif isinstance(node, list):
        for item in node:
            find_refs(item, refs)


def extract_operation_with_refs(spec: dict, path: str, method: str, op: dict) -> dict:
    """Return an operation and the local definitions reachable from its refs."""
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
        to_process |= nested - seen

    security_schemes = spec.get("components", {}).get("securitySchemes", {})
    if not security_schemes and isinstance(spec.get("securityDefinitions"), dict):
        security_schemes = spec["securityDefinitions"]
    return {
        "path": path,
        "method": method.upper(),
        "operation": op,
        "definitions": definitions,
        "security_schemes": security_schemes,
    }


def _merge_parameters(spec: dict, path_item: dict, operation: dict) -> dict:
    merged = {}
    for parameter in [*path_item.get("parameters", []), *operation.get("parameters", [])]:
        resolved = parameter
        if isinstance(parameter, dict) and isinstance(parameter.get("$ref"), str):
            resolved = resolve_ref(spec, parameter["$ref"])
        if isinstance(resolved, dict) and "name" in resolved and "in" in resolved:
            merged[(resolved["name"], resolved["in"])] = parameter
        else:
            try:
                identity = json.dumps(parameter, sort_keys=True)
            except TypeError:
                identity = repr(parameter)
            merged[("unidentified", identity)] = parameter
    effective = dict(operation)
    if merged:
        effective["parameters"] = list(merged.values())
    if "security" not in effective and "security" in spec:
        effective["security"] = spec["security"]
    return effective


def _ingest_openapi(spec: dict, spec_path: str) -> list[dict]:
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        raise ValueError(f"Invalid OpenAPI source {spec_path!r}: 'paths' must be an object")

    operations = []
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            raise ValueError(f"Invalid OpenAPI path item {path!r}: expected an object")
        for method, operation in path_item.items():
            if str(method).lower() not in HTTP_METHODS:
                continue
            if not isinstance(operation, dict):
                raise ValueError(
                    f"Invalid OpenAPI operation {str(method).upper()} {path}: expected an object"
                )
            effective = _merge_parameters(spec, path_item, operation)
            operations.append(extract_operation_with_refs(spec, str(path), str(method), effective))
    return operations


def _is_postman_collection(document: dict) -> bool:
    info = document.get("info")
    if not isinstance(info, dict) or not isinstance(document.get("item"), list):
        return False
    schema = str(info.get("schema") or "").lower()
    return bool(document.get("_postman_id") or info.get("_postman_id") or "postman" in schema)


def ingest_openapi_spec(spec_path: str) -> list[dict]:
    """Auto-detect and ingest OpenAPI JSON/YAML or Postman Collection JSON."""
    document = _load_document(spec_path)
    if "openapi" in document or "swagger" in document:
        return _ingest_openapi(document, spec_path)
    if _is_postman_collection(document):
        return ingest_postman_collection(document)
    raise ValueError(
        f"Unsupported API source {spec_path!r}: expected an OpenAPI/Swagger document "
        "or a Postman Collection v2.0/v2.1 document"
    )


def pretty_print_operations(operations: list[dict]) -> None:
    """Pretty-print extracted operations to the console."""
    method_colors = {
        "GET": "🟢",
        "POST": "🟡",
        "PUT": "🔵",
        "PATCH": "🟣",
        "DELETE": "🔴",
    }
    print(f"\n{'=' * 70}")
    print(f"OPERATIONS ({len(operations)} total)")
    print(f"{'=' * 70}")
    for index, wrapper in enumerate(operations, start=1):
        operation = wrapper.get("operation", {})
        method = wrapper.get("method", "")
        icon = method_colors.get(method, "⚪")
        print(f"\n[{index}] {icon} {method:6} {wrapper.get('path')}")
        print(f"    {operation.get('summary') or '(no summary)'}")
    print(f"\n{'=' * 70}\n")
