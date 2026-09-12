from google import genai
from google.genai import types
from config.settings import settings
from database.firestore_client import get_session
from utils.logger import get_logger
import json

logger = get_logger(__name__)

MODEL_ID = "gemini-3.6-flash"
HTTP_OPTIONS = types.HttpOptions(timeout=settings.API_TIMEOUT * 1000)
client = None
QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "difficulty": {
                        "type": "string",
                        "enum": ["Beginner", "Intermediate", "Advanced"],
                    },
                },
                "required": ["question", "difficulty"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}

LEVEL_SCHEMA = {
    "type": "object",
    "properties": {
        "validated_level": {
            "type": "string",
            "enum": ["Beginner", "Intermediate", "Advanced"],
        },
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": ["validated_level", "confidence", "reasoning"],
    "additionalProperties": False,
}


def _get_client():
    global client
    if client is None:
        client = genai.Client(
            api_key=settings.GEMINI_API_KEY,
            http_options=HTTP_OPTIONS,
        )
    return client


def _generate_json(prompt: str, schema: dict, *, max_output_tokens: int):
    response = _get_client().models.generate_content(
        model=MODEL_ID,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.1,
            top_p=0.8,
            max_output_tokens=max_output_tokens,
            response_mime_type="application/json",
            response_json_schema=schema,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            http_options=HTTP_OPTIONS,
        ),
    )
    if getattr(response, "parsed", None) is not None:
        return response.parsed
    return json.loads(response.text)

def generate_assessment_questions(skill, declared_level, session_id):
    try:
        prompt = f"""Generate EXACTLY 3 short {declared_level} level validation questions about {skill}.

Return ONLY valid JSON (no markdown, no code blocks):
{{
  "questions": [
    {{"question": "...", "difficulty": "{declared_level}"}},
    {{"question": "...", "difficulty": "{declared_level}"}},
    {{"question": "...", "difficulty": "{declared_level}"}}
  ]
}}"""

        result = _generate_json(prompt, QUESTION_SCHEMA, max_output_tokens=192)
        result["skill"] = skill
        result["declared_level"] = declared_level
        
        logger.info(f"Assessment questions generated for {session_id}")
        return result
        
    except Exception as e:
        logger.error(f"Error in generate_assessment_questions: {e}")
        return {"error": str(e), "questions": []}

def validate_user_level(session_id, answers, questions=None, declared_level=None):
    try:
        resolved_questions = questions or []
        resolved_level = declared_level or "Beginner"

        if not resolved_questions:
            session = get_session(session_id)
            if not session:
                logger.error(f"Session not found: {session_id}")
                return {"error": "Session not found"}

            resolved_questions = session.get('assessment_questions', [])
            resolved_level = session.get('declared_level', resolved_level)

        prompt = f"""User declared level: {resolved_level}

Questions and answers:
{json.dumps([{'q': q['question'], 'a': a} for q, a in zip(resolved_questions, answers)], indent=2)}

Validate whether the answers match the declared level. Keep reasoning under 20 words.
Return ONLY valid JSON (no markdown):
{{
  "validated_level": "Beginner/Intermediate/Advanced",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation"
}}"""

        result = _generate_json(prompt, LEVEL_SCHEMA, max_output_tokens=128)
        result["declared_level"] = resolved_level
        result["question_count"] = len(resolved_questions)
        
        logger.info(f"Level validated for {session_id}")
        return result
        
    except Exception as e:
        logger.error(f"Error in validate_user_level: {e}")
        return {"error": str(e), "validated_level": "Beginner", "confidence": 0.5}

TOOL_DEFINITIONS = [
    {
        "name": "generate_assessment_questions",
        "description": "Generate skill assessment questions based on declared user level",
        "parameters": {
            "type": "object",
            "properties": {
                "skill": {"type": "string", "description": "The skill to assess"},
                "declared_level": {"type": "string", "enum": ["Beginner", "Intermediate", "Advanced"], "description": "User's declared skill level"},
                "session_id": {"type": "string", "description": "User session ID"}
            },
            "required": ["skill", "declared_level", "session_id"]
        }
    },
    {
        "name": "validate_user_level",
        "description": "Validate user's skill level based on their answers to assessment questions",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "User session ID"},
                "answers": {"type": "array", "items": {"type": "string"}, "description": "User's answers to assessment questions"},
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"},
                            "difficulty": {"type": "string"}
                        }
                    },
                    "description": "Optional assessment questions supplied by the caller to avoid a database lookup"
                },
                "declared_level": {
                    "type": "string",
                    "enum": ["Beginner", "Intermediate", "Advanced"],
                    "description": "Optional declared level supplied by the caller to avoid a database lookup"
                }
            },
            "required": ["session_id", "answers"]
        }
    }
]
