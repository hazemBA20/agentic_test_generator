"""Pytest configuration for generated API tests.

Credential checks happen per plan in ``send_request`` because OpenAPI operations
may be public, API-key protected, bearer protected, or require both.
"""