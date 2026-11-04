"""Generate OpenAPI error responses from the same catalog used at runtime."""
from app.core.error_codes import ErrorCode
from app.schemas.common import ErrorEnvelope


def error_responses() -> dict:
    responses = {}
    for code in ErrorCode:
        definition = code.definition
        response = responses.setdefault(definition.status, {
            "model": ErrorEnvelope,
            "description": "",
            "content": {"application/json": {"examples": {}}},
        })
        response["description"] += f"{code.value}: {definition.message}；"
        response["content"]["application/json"]["examples"][code.value] = {
            "summary": definition.message,
            "value": {"code": code.value, "message": definition.message, "data": None,
                      "error": {"details": []}, "request_id": "example-request-id",
                      "timestamp": "2026-10-21T00:00:00Z"},
        }
    return responses
