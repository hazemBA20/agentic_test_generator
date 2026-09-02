"""Normalize Postman Collection v2 documents into OpenAPI-like operations."""

import json
import re
from urllib.parse import parse_qsl, urlsplit


_VARIABLE = re.compile(r"{{\s*([^{}]+?)\s*}}")
_PATH_VARIABLE = re.compile(r"^:([^/]+)$")
_AUTH_HEADERS = {"authorization", "proxy-authorization", "x-api-key"}


def infer_schema(value) -> dict:
    """Infer a small JSON Schema without treating observed properties as required."""
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        schema = {"type": "array"}
        if value:
            schema["items"] = infer_schema(value[0])
        return schema
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {key: infer_schema(item) for key, item in value.items()},
        }
    return {"type": "string"}


def _description(value) -> str | None:
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        content = value.get("content")
        return content if isinstance(content, str) and content else None
    return None


def _enabled(entries) -> list[dict]:
    return [entry for entry in entries or [] if isinstance(entry, dict) and not entry.get("disabled")]


def _variables(entries) -> dict[str, object]:
    values = {}
    for variable in _enabled(entries):
        key = variable.get("key") or variable.get("id")
        if key and "value" in variable:
            values[str(key)] = variable["value"]
    return values


def _resolve_variables(value, variables: dict[str, object]):
    if not isinstance(value, str):
        return value

    exact = _VARIABLE.fullmatch(value)
    if exact and exact.group(1) in variables:
        return variables[exact.group(1)]

    def replace(match):
        replacement = variables.get(match.group(1), match.group(0))
        return str(replacement)

    resolved = value
    for _ in range(10):
        updated = _VARIABLE.sub(replace, resolved)
        if updated == resolved:
            break
        resolved = updated
    return resolved


def _example(value, variables: dict[str, object]):
    resolved = _resolve_variables(value, variables)
    if not isinstance(resolved, str) or _VARIABLE.search(resolved):
        return resolved
    stripped = resolved.strip()
    if stripped.lower() == "true":
        return True
    if stripped.lower() == "false":
        return False
    if stripped.lower() == "null":
        return None
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return resolved
    return parsed if isinstance(parsed, (int, float, list, dict)) else resolved


def _parameter(name, location, value, variables, *, required=False, description=None) -> dict:
    example = _example(value, variables)
    parameter = {
        "name": str(name),
        "in": location,
        "required": True if location == "path" else bool(required),
        "schema": infer_schema(example),
        "example": example,
    }
    if description:
        parameter["description"] = description
    return parameter


def _string_url_parts(raw_url: str) -> tuple[list[str], list[dict]]:
    raw_url = raw_url.strip()
    # An unresolved host variable is configuration, not an operation path segment.
    raw_url = re.sub(r"^(?:https?://)?{{[^{}]+}}(?=/|$)", "", raw_url)
    parsed = urlsplit(raw_url if "://" in raw_url else f"http://postman.invalid/{raw_url.lstrip('/')}")
    path = [segment for segment in parsed.path.split("/") if segment]
    query = [{"key": key, "value": value} for key, value in parse_qsl(parsed.query, keep_blank_values=True)]
    return path, query


def _url_parts(url) -> tuple[list, list[dict], list[dict]]:
    if isinstance(url, str):
        path, query = _string_url_parts(url)
        return path, query, []
    if not isinstance(url, dict):
        return [], [], []

    path = url.get("path")
    if not isinstance(path, list):
        path, parsed_query = _string_url_parts(str(url.get("raw") or ""))
    else:
        parsed_query = []
    query = url.get("query") if isinstance(url.get("query"), list) else parsed_query
    return path, query, url.get("variable") or []


def _normalize_path(url, variables) -> tuple[str, list[dict]]:
    segments, query, url_variables = _url_parts(url)
    path_values = _variables(url_variables)
    normalized = []
    path_parameters = []
    for raw_segment in segments:
        segment = raw_segment.get("value", "") if isinstance(raw_segment, dict) else str(raw_segment)
        match = _PATH_VARIABLE.match(segment)
        brace_match = re.fullmatch(r"{([^{}]+)}", segment)
        name = match.group(1) if match else brace_match.group(1) if brace_match else None
        if name:
            normalized.append("{" + name + "}")
            value = path_values.get(name, variables.get(name, "{{" + name + "}}"))
            path_parameters.append(_parameter(name, "path", value, variables, required=True))
        elif _VARIABLE.fullmatch(segment):
            # Host/base URL placeholders occasionally leak into structured paths.
            continue
        else:
            normalized.append(segment)
    return "/" + "/".join(normalized), [*path_parameters, *query]


def _request_headers(headers, variables) -> tuple[list[dict], dict[str, str]]:
    parameters = []
    values = {}
    for header in _enabled(headers):
        name = header.get("key")
        if not name:
            continue
        values[str(name).lower()] = str(header.get("value", ""))
        if str(name).lower() in _AUTH_HEADERS:
            continue
        parameters.append(
            _parameter(
                name,
                "header",
                header.get("value", ""),
                variables,
                required=header.get("required", False),
                description=header.get("description"),
            )
        )
    return parameters, values


def _object_body(entries, variables, *, multipart=False) -> dict:
    properties = {}
    required = []
    for field in _enabled(entries):
        name = field.get("key")
        if not name:
            continue
        if multipart and field.get("type") == "file":
            schema = {"type": "string", "format": "binary"}
        else:
            example = _example(field.get("value", ""), variables)
            schema = {**infer_schema(example), "example": example}
        if field.get("description"):
            schema["description"] = field["description"]
        properties[str(name)] = schema
        if field.get("required"):
            required.append(str(name))
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _request_body(body, headers, variables) -> dict | None:
    if not isinstance(body, dict) or body.get("disabled"):
        return None
    mode = body.get("mode")
    content = None
    if mode == "raw":
        raw = _resolve_variables(body.get("raw", ""), variables)
        language = ((body.get("options") or {}).get("raw") or {}).get("language")
        is_json = language == "json" or "json" in headers.get("content-type", "").lower()
        if is_json and isinstance(raw, str):
            try:
                example = json.loads(raw)
            except json.JSONDecodeError:
                is_json = False
                example = raw
        else:
            example = raw
        media_type = "application/json" if is_json else headers.get("content-type", "text/plain")
        content = {media_type: {"schema": infer_schema(example), "example": example}}
    elif mode == "formdata":
        content = {"multipart/form-data": {"schema": _object_body(body.get("formdata"), variables, multipart=True)}}
    elif mode == "urlencoded":
        content = {
            "application/x-www-form-urlencoded": {
                "schema": _object_body(body.get("urlencoded"), variables)
            }
        }
    if content is None:
        return None
    request_body = {"content": content}
    if body.get("description"):
        request_body["description"] = body["description"]
    return request_body


def _response_content_type(response) -> str:
    for header in _enabled(response.get("header")):
        if str(header.get("key", "")).lower() == "content-type":
            return str(header.get("value", "application/json")).split(";", 1)[0].strip()
    return "application/json"


def _responses(saved_responses, variables) -> dict:
    responses = {}
    for response in saved_responses or []:
        if not isinstance(response, dict) or response.get("code") is None:
            continue
        code = str(response["code"])
        normalized = {"description": response.get("status") or response.get("name") or "Saved response"}
        body = _resolve_variables(response.get("body"), variables)
        if body not in (None, ""):
            media_type = _response_content_type(response)
            example = body
            if "json" in media_type and isinstance(body, str):
                try:
                    example = json.loads(body)
                except json.JSONDecodeError:
                    pass
            normalized["content"] = {
                media_type: {"schema": infer_schema(example), "example": example}
            }
        responses[code] = normalized
    if not responses:
        responses["200"] = {
            "description": "Inferred success response; the collection has no saved response."
        }
    return responses


def _operation(item, request, variables, folders) -> dict:
    method = str(request.get("method") or "GET").upper()
    path, path_and_query = _normalize_path(request.get("url"), variables)
    path_parameters = [entry for entry in path_and_query if "in" in entry]
    query_entries = [entry for entry in path_and_query if "in" not in entry]
    parameters = list(path_parameters)
    for query in _enabled(query_entries):
        name = query.get("key")
        if name:
            parameters.append(
                _parameter(
                    name,
                    "query",
                    query.get("value", ""),
                    variables,
                    required=query.get("required", False),
                    description=query.get("description"),
                )
            )
    header_parameters, header_values = _request_headers(request.get("header"), variables)
    parameters.extend(header_parameters)

    operation = {
        "summary": str(item.get("name") or f"{method} {path}"),
        "responses": _responses(item.get("response"), variables),
    }
    description = _description(request.get("description")) or _description(item.get("description"))
    if description:
        operation["description"] = description
    if parameters:
        operation["parameters"] = parameters
    request_body = _request_body(request.get("body"), header_values, variables)
    if request_body:
        operation["requestBody"] = request_body

    wrapper = {
        "path": path,
        "method": method,
        "operation": operation,
        "definitions": {},
        "security_schemes": {},
        "source": {"type": "postman", "item_name": item.get("name")},
    }
    if folders:
        wrapper["source"]["folders"] = folders
    return wrapper


def ingest_postman_collection(collection: dict) -> list[dict]:
    variables = _variables(collection.get("variable"))
    operations = []

    def visit(items, folders):
        for item in items or []:
            if not isinstance(item, dict):
                continue
            request = item.get("request")
            if isinstance(request, str):
                request = {"url": request, "method": "GET"}
            if isinstance(request, dict):
                operations.append(_operation(item, request, variables, folders))
            if isinstance(item.get("item"), list):
                visit(item["item"], [*folders, str(item.get("name") or "")])

    visit(collection.get("item"), [])
    return operations
