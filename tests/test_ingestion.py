import json
from pathlib import Path

import pytest

from src.helpers import _test_support
from src.helpers.parser import ingest_openapi_spec, resolve_ref


def _write_json(tmp_path: Path, name: str, document: dict) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _postman(items, variables=None):
    return {
        "info": {
            "_postman_id": "collection-id",
            "name": "Example API",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "variable": variables or [],
        "item": items,
    }


def test_yaml_openapi_matches_json_normalization_with_refs_and_path_parameters(tmp_path: Path):
    yaml_path = tmp_path / "api.yaml"
    yaml_path.write_text(
        """
openapi: 3.0.3
security:
  - ApiKey: []
components:
  securitySchemes:
    ApiKey:
      type: apiKey
      in: header
      name: X-API-Key
  parameters:
    PetId:
      name: petId
      in: path
      required: true
      schema:
        type: integer
  schemas:
    Pet:
      type: object
      properties:
        id:
          type: integer
paths:
  /pets/{petId}:
    parameters:
      - $ref: '#/components/parameters/PetId'
    get:
      summary: Get pet
      responses:
        '200':
          description: Found
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Pet'
""".lstrip(),
        encoding="utf-8",
    )

    operation = ingest_openapi_spec(str(yaml_path))[0]

    assert operation["path"] == "/pets/{petId}"
    assert operation["method"] == "GET"
    assert operation["operation"]["parameters"] == [
        {"$ref": "#/components/parameters/PetId"}
    ]
    assert operation["operation"]["security"] == [{"ApiKey": []}]
    assert operation["security_schemes"]["ApiKey"]["type"] == "apiKey"
    assert set(operation["definitions"]) == {
        "#/components/parameters/PetId",
        "#/components/schemas/Pet",
    }


def test_json_openapi_regression_and_operation_parameter_override(tmp_path: Path):
    path = _write_json(
        tmp_path,
        "api.json",
        {
            "openapi": "3.0.0",
            "paths": {
                "/search": {
                    "parameters": [
                        {"name": "limit", "in": "query", "schema": {"type": "integer"}}
                    ],
                    "get": {
                        "parameters": [
                            {
                                "name": "limit",
                                "in": "query",
                                "schema": {"type": "integer"},
                                "example": 10,
                            }
                        ],
                        "responses": {"200": {"description": "OK"}},
                    },
                }
            },
        },
    )

    operation = ingest_openapi_spec(str(path))[0]

    assert operation["method"] == "GET"
    assert operation["operation"]["parameters"][0]["example"] == 10
    assert operation["definitions"] == {}


def test_local_json_pointer_decodes_escaped_slash_and_tilde():
    target = {"type": "string"}
    spec = {"components": {"schemas": {"a/b~c": target}}}

    assert resolve_ref(spec, "#/components/schemas/a~1b~0c") is target


def test_nested_postman_folder_normalizes_url_parameters_headers_and_saved_response(tmp_path: Path):
    collection = _postman(
        [
            {
                "name": "Users",
                "item": [
                    {
                        "name": "Get user",
                        "request": {
                            "method": "get",
                            "description": "Fetch one user",
                            "header": [
                                {"key": "X-Trace", "value": "{{trace}}", "required": True},
                                {"key": "Authorization", "value": "Bearer secret"},
                                {"key": "X-Disabled", "value": "no", "disabled": True},
                            ],
                            "url": {
                                "raw": "{{baseUrl}}/users/:id?expand={{expand}}",
                                "path": ["{{baseUrl}}", "users", ":id"],
                                "query": [
                                    {"key": "expand", "value": "{{expand}}"},
                                    {"key": "ignored", "value": "x", "disabled": True},
                                ],
                                "variable": [{"key": "id", "value": "{{userId}}"}],
                            },
                        },
                        "response": [
                            {
                                "name": "Found",
                                "status": "OK",
                                "code": 200,
                                "header": [{"key": "Content-Type", "value": "application/json; charset=utf-8"}],
                                "body": '{"id":42,"active":true,"score":1.5,"tags":["admin"]}',
                            }
                        ],
                    }
                ],
            }
        ],
        variables=[
            {"key": "baseUrl", "value": "https://api.example.test/v1"},
            {"key": "userId", "value": "42"},
            {"key": "expand", "value": "profile"},
        ],
    )
    path = _write_json(tmp_path, "collection.json", collection)

    wrapper = ingest_openapi_spec(str(path))[0]
    operation = wrapper["operation"]

    assert wrapper["path"] == "/users/{id}"
    assert wrapper["method"] == "GET"
    assert wrapper["source"]["folders"] == ["Users"]
    parameters = {(parameter["name"], parameter["in"]): parameter for parameter in operation["parameters"]}
    assert parameters[("id", "path")]["example"] == 42
    assert parameters[("id", "path")]["required"] is True
    assert parameters[("expand", "query")]["example"] == "profile"
    assert parameters[("X-Trace", "header")]["example"] == "{{trace}}"
    assert parameters[("X-Trace", "header")]["required"] is True
    assert ("Authorization", "header") not in parameters
    response = operation["responses"]["200"]["content"]["application/json"]
    assert response["example"]["active"] is True
    assert response["schema"]["properties"]["active"] == {"type": "boolean"}
    assert response["schema"]["properties"]["score"] == {"type": "number"}
    assert "required" not in response["schema"]


def test_postman_string_url_and_raw_json_body_infer_recursive_schema(tmp_path: Path):
    path = _write_json(
        tmp_path,
        "raw.json",
        _postman(
            [
                {
                    "name": "Create widget",
                    "request": {
                        "method": "POST",
                        "url": "{{baseUrl}}/widgets?dryRun=true",
                        "header": [{"key": "Content-Type", "value": "application/json"}],
                        "body": {
                            "mode": "raw",
                            "raw": '{"enabled":true,"count":2,"metadata":{"owner":null},"values":[1]}',
                            "options": {"raw": {"language": "json"}},
                        },
                    },
                }
            ],
            variables=[{"key": "baseUrl", "value": "https://example.test"}],
        ),
    )

    wrapper = ingest_openapi_spec(str(path))[0]
    operation = wrapper["operation"]
    media = operation["requestBody"]["content"]["application/json"]

    assert wrapper["path"] == "/widgets"
    assert next(p for p in operation["parameters"] if p["in"] == "query")["example"] is True
    assert media["example"]["metadata"]["owner"] is None
    properties = media["schema"]["properties"]
    assert properties["enabled"] == {"type": "boolean"}
    assert properties["count"] == {"type": "integer"}
    assert properties["metadata"]["properties"]["owner"] == {"type": "null"}
    assert properties["values"]["items"] == {"type": "integer"}
    assert "required" not in media["schema"]
    assert operation["responses"] == {
        "200": {"description": "Inferred success response; the collection has no saved response."}
    }


def test_postman_multipart_and_urlencoded_body_forms(tmp_path: Path):
    path = _write_json(
        tmp_path,
        "forms.json",
        _postman(
            [
                {
                    "name": "Upload",
                    "request": {
                        "method": "POST",
                        "url": {"path": ["uploads"]},
                        "body": {
                            "mode": "formdata",
                            "formdata": [
                                {"key": "document", "type": "file", "src": "ignored.pdf", "required": True},
                                {"key": "label", "type": "text", "value": "invoice"},
                                {"key": "skip", "type": "text", "value": "x", "disabled": True},
                            ],
                        },
                    },
                },
                {
                    "name": "Sign in",
                    "request": {
                        "method": "POST",
                        "url": {"path": ["sessions"]},
                        "body": {
                            "mode": "urlencoded",
                            "urlencoded": [
                                {"key": "username", "value": "alice", "required": True},
                                {"key": "unused", "value": "x", "disabled": True},
                            ],
                        },
                    },
                },
            ]
        ),
    )

    upload, sign_in = ingest_openapi_spec(str(path))
    upload_schema = upload["operation"]["requestBody"]["content"]["multipart/form-data"]["schema"]
    encoded_schema = sign_in["operation"]["requestBody"]["content"][
        "application/x-www-form-urlencoded"
    ]["schema"]

    assert upload_schema["properties"]["document"] == {"type": "string", "format": "binary"}
    assert upload_schema["required"] == ["document"]
    assert "skip" not in upload_schema["properties"]
    assert encoded_schema["properties"]["username"]["example"] == "alice"
    assert encoded_schema["required"] == ["username"]


def test_postman_request_may_be_a_string(tmp_path: Path):
    path = _write_json(
        tmp_path,
        "string-request.json",
        _postman([{"name": "Health", "request": "https://example.test/health"}]),
    )

    operation = ingest_openapi_spec(str(path))[0]

    assert operation["path"] == "/health"
    assert operation["method"] == "GET"


def test_urlencoded_runtime_uses_data_and_json_behavior_is_unchanged(monkeypatch):
    captured = []

    def fake_request(**kwargs):
        captured.append(kwargs)
        return object()

    monkeypatch.setattr(_test_support.requests, "request", fake_request)
    monkeypatch.setattr(_test_support, "BASE_URL", "https://example.test")

    _test_support.send_request(
        "POST", "/sessions", {"username": "alice"}, "application/x-www-form-urlencoded"
    )
    _test_support.send_request("POST", "/widgets", {"name": "one"}, "application/json")

    assert captured[0]["data"] == {"username": "alice"}
    assert "json" not in captured[0]
    assert captured[1]["json"] == {"name": "one"}
    assert "data" not in captured[1]


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("scalar.yaml", "- not\n- a\n- mapping\n", "document root"),
        ("broken.yaml", "openapi: [", "Invalid JSON/YAML"),
        ("unknown.json", '{"name":"not an API"}', "Unsupported API source"),
    ],
)
def test_malformed_or_unsupported_documents_have_actionable_errors(
    tmp_path: Path, name: str, content: str, message: str
):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        ingest_openapi_spec(str(path))
