import requests
from pydantic import BaseModel, Field

from config.settings import settings
from executor_service.runtime import (
    execute_code as execute_code_local,
    get_execution_config as get_execution_config_local,
    normalize_language,
    validate_code as validate_code_local,
)
from utils.logger import get_logger

logger = get_logger(__name__)


class GeminiError(BaseModel):
    line: int | None = Field(None, description="Line number when identifiable")
    message: str = Field(..., description="Specific error")

class GeminiValidationResult(BaseModel):
    status: str = Field(..., description="Exactly 'VALID' or 'INVALID'")
    errors: list[GeminiError] = Field(..., description="List of concrete errors, empty if VALID")
    explanation: str = Field(..., description="Brief explanation")
    suggestion: str = Field(..., description="Correction guidance")


def _is_environment_error(result: dict) -> bool:
    if result.get("type") == "unsupported_language":
        return True
    
    if result.get("type") not in ("runtime_error", "compilation_error"):
        return False
        
    err = (result.get("error") or "").lower()
    stderr = (result.get("stderr") or "").lower()
    combined = err + "\n" + stderr
    
    if "modulenotfounderror" in combined or "no module named" in combined or "importerror" in combined:
        return True
    if "module_not_found" in combined or "cannot find module" in combined or "cannot find package" in combined:
        return True
    if "fatal error:" in combined and "no such file or directory" in combined:
        return True
    if "cannot find -l" in combined:
        return True
    if "command not found" in combined or "executable file not found" in combined:
        return True
        
    return False


def _gemini_fallback_validation(code: str, language: str, context: dict) -> dict:
    try:
        from utils.gemini_client_pool import PooledGemini
        client = PooledGemini()
        
        prompt = f"""You are a strict code validator.
Does this code satisfy the student's task requirement based on the context?

Language: {language}

Submitted Code:
{code}

Learning Context / Task Requirement:
{context}

Return structured JSON evaluating if it is VALID or INVALID."""

        response = client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt,
            config={
                'response_mime_type': 'application/json',
                'response_schema': GeminiValidationResult,
            },
        )
        
        parsed = response.parsed
        if not parsed:
            raise ValueError("Empty parsed response from Gemini")

        return {
            "success": True,
            "type": "ai_fallback",
            "fallback": True,
            "validation": {
                "status": parsed.status,
                "errors": [e.model_dump() for e in parsed.errors] if parsed.errors else [],
                "explanation": parsed.explanation,
                "suggestion": parsed.suggestion
            }
        }
    except Exception as e:
        logger.error(f"Gemini fallback validation failed: {e}")
        return {
            "success": False,
            "type": "ai_fallback",
            "fallback": True,
            "validation": {
                "status": "UNAVAILABLE",
                "errors": [],
                "explanation": "AI Code Validation: Unavailable",
                "suggestion": "Validation failed due to a system error."
            }
        }


def _executor_headers() -> dict:
    headers = {}
    if settings.EXECUTOR_SHARED_SECRET:
        headers["X-Executor-Secret"] = settings.EXECUTOR_SHARED_SECRET
    return headers


def _call_executor_service(path: str, method: str = "GET", payload: dict | None = None) -> dict:
    base_url = (settings.EXECUTOR_SERVICE_URL or "").rstrip("/")
    if not base_url:
        raise ValueError("EXECUTOR_SERVICE_URL is not configured")

    url = f"{base_url}{path}"
    timeout = settings.EXECUTOR_REQUEST_TIMEOUT

    if method == "GET":
        response = requests.get(url, headers=_executor_headers(), timeout=timeout)
    else:
        response = requests.post(url, json=payload or {}, headers=_executor_headers(), timeout=timeout)

    response.raise_for_status()
    body = response.json()
    if not body.get("success", False):
        raise RuntimeError(body.get("error", "Executor service request failed"))
    return body.get("data", {})


def execute_code(code: str, language: str, stdin: str = "", context: dict = None) -> dict:
    """Execute code using the executor service, falling back to Gemini if environment unavailable."""
    normalized = normalize_language(language)
    result = None
    
    try:
        if settings.EXECUTOR_SERVICE_URL:
            result = _call_executor_service(
                "/execute",
                method="POST",
                payload={
                    "code": code,
                    "language": normalized,
                    "stdin": stdin,
                },
            )
    except Exception as e:
        logger.warning(f"Executor service call failed, using local runtime fallback: {e}")
        
    if not result:
        result = execute_code_local(code=code, language=normalized, stdin=stdin)

    if _is_environment_error(result):
        logger.info(f"Executor environment unavailable for {language}, triggering Gemini fallback.")
        return _gemini_fallback_validation(code, language, context or {})

    return result


def get_execution_config() -> dict:
    """Return editor/runtime config for the coding environment."""
    try:
        if settings.EXECUTOR_SERVICE_URL:
            return _call_executor_service("/config", method="GET")
    except Exception as e:
        logger.warning(f"Execution config service call failed, using local config fallback: {e}")

    return get_execution_config_local()


def validate_code(code: str, language: str) -> dict:
    """Validate code syntax using the executor service, falling back locally."""
    normalized = normalize_language(language)
    try:
        if settings.EXECUTOR_SERVICE_URL:
            return _call_executor_service(
                "/validate",
                method="POST",
                payload={
                    "code": code,
                    "language": normalized,
                },
            )
    except Exception as e:
        logger.warning(f"Validation service call failed, using local validation fallback: {e}")

    return validate_code_local(code=code, language=normalized)


TOOL_DEFINITIONS = [
    {
        "name": "execute_code",
        "description": "Execute code in various programming languages using the isolated executor service",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Source code to execute"},
                "language": {"type": "string", "description": "Programming language"},
                "stdin": {"type": "string", "description": "Standard input for the code"},
                "context": {"type": "object", "description": "Task context to validate against if execution fails"},
            },
            "required": ["code", "language"],
        },
    },
    {
        "name": "validate_code",
        "description": "Validate code syntax without executing",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Source code to validate"},
                "language": {"type": "string", "description": "Programming language"},
            },
            "required": ["code", "language"],
        },
    },
    {
        "name": "get_execution_config",
        "description": "Return the coding environment config for Monaco and the supported execution runtimes.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]
