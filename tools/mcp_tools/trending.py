import concurrent.futures
import json
import threading
import time

from google import genai
from google.genai import types

from config.settings import settings
from database.firestore_client import get_cache, set_cache
from utils.gemini_client_pool import PooledGemini
from utils.logger import get_logger

logger = get_logger(__name__)

MODEL_ID = "gemini-3.6-flash"
HTTP_OPTIONS = types.HttpOptions(timeout=settings.API_TIMEOUT * 1000)
client = None
_cache_lock = threading.Lock()
_local_cache = {}
_cache_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="trending-cache",
)


def _get_client():
    global client
    if client is None:
        client = PooledGemini()
    return client


def _get_local_cache(cache_key: str):
    with _cache_lock:
        cached = _local_cache.get(cache_key)
        if not cached:
            return None
        if cached["expires_at"] < time.time():
            _local_cache.pop(cache_key, None)
            return None
        return cached["value"]


def _set_local_cache(cache_key: str, value):
    with _cache_lock:
        _local_cache[cache_key] = {
            "value": value,
            "expires_at": time.time() + (settings.TRENDING_CACHE_TTL * 3600),
        }


def _get_fast_cache(cache_key: str):
    local = _get_local_cache(cache_key)
    if local:
        logger.info(f"Local trending cache hit: {cache_key}")
        return local

    future = _cache_executor.submit(get_cache, cache_key)
    try:
        cached = future.result(timeout=min(2.0, max(1.0, settings.API_TIMEOUT / 10)))
    except Exception as e:
        logger.warning(f"Skipping slow Firestore cache read for {cache_key}: {e}")
        return None

    if cached:
        _set_local_cache(cache_key, cached)
        logger.info(f"Firestore trending cache hit: {cache_key}")
    return cached


def _store_cache_async(cache_key: str, skills):
    _set_local_cache(cache_key, skills)
    _cache_executor.submit(set_cache, cache_key, skills, settings.TRENDING_CACHE_TTL)


def _normalize_skill_name(skill: str):
    normalized = " ".join((skill or "").split()).strip(" ,.-")
    if not normalized:
        return ""

    aliases = {
        "js": "JavaScript",
        "ts": "TypeScript",
        "reactjs": "React",
        "nodejs": "Node.js",
        "postgres": "PostgreSQL",
        "ci cd": "CI/CD",
        "rest api": "REST API",
        "llms": "Large Language Models",
        "rag": "Retrieval-Augmented Generation",
        "nlp": "Natural Language Processing",
    }
    lowered = normalized.lower()
    return aliases.get(lowered, normalized)


def _build_role_trending_from_live_jobs(target_role: str):
    from tools.mcp_tools.resume import _extract_job_skills, _fetch_jobs

    jobs = _fetch_jobs(target_role)
    if not jobs:
        return {"error": "Unable to fetch live jobs for role", "skills": [], "role": target_role}

    extracted_skills = _extract_job_skills(jobs)
    normalized_skills = []
    seen = set()
    for raw_skill in extracted_skills:
        skill = _normalize_skill_name(raw_skill)
        if not skill:
            continue
        key = skill.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized_skills.append(skill)

    if not normalized_skills:
        return {"error": "Unable to extract trending skills from live jobs", "skills": [], "role": target_role}

    role_texts = []
    for job in jobs:
        parts = [job.get("job_title", ""), job.get("job_description", "")]
        highlights = job.get("job_highlights") or {}
        for lines in highlights.values():
            if isinstance(lines, list):
                parts.extend(str(line) for line in lines)
        role_texts.append(" ".join(parts).lower())

    results = []
    for idx, skill in enumerate(normalized_skills[:7]):
        skill_lower = skill.lower()
        mentions = 0
        for job_text in role_texts:
            if skill_lower in job_text:
                mentions += 1

        if mentions >= 3 or idx < 2:
            demand = "Very High"
        elif mentions == 2 or idx < 5:
            demand = "High"
        else:
            demand = "Medium"

        if mentions > 0:
            reason = f"Mentioned in {mentions} live job posting{'s' if mentions != 1 else ''}"
        elif idx < 3:
            reason = "Repeated across current live job requirements"
        else:
            reason = "Present in current live role requirements"

        results.append(
            {
                "skill": skill,
                "demand": demand,
                "reason": reason,
            }
        )

    return {"skills": results, "role": target_role, "count": len(results)}


def _generate_general_trending():
    prompt = """Return EXACTLY 7 trending technical skills for 2026.

Return ONLY a JSON array of skill names (no markdown, no code blocks):
["Python", "Docker", "Kubernetes", "..."]
"""

    response = _get_client().models.generate_content(
        model=MODEL_ID,
        contents=prompt,
        config=types.GenerateContentConfig(
            max_output_tokens=2048,
            response_mime_type="application/json",
            response_json_schema={
                "type": "array",
                "items": {"type": "string"},
                "minItems": 7,
                "maxItems": 7,
            },
            http_options=types.HttpOptions(timeout=settings.API_TIMEOUT * 1000),
        ),
    )

    parsed = getattr(response, "parsed", None)
    if parsed is None:
        text = (getattr(response, "text", "") or "").strip()
        text = text.replace("```json", "").replace("```", "").strip()
        if not text:
            try:
                finish_reason = response.candidates[0].finish_reason
            except (IndexError, AttributeError):
                finish_reason = "UNKNOWN"
            raise ValueError(f"Model returned empty response. Finish reason: {finish_reason}")
        if not text:
            try:
                finish_reason = response.candidates[0].finish_reason
            except (IndexError, AttributeError):
                finish_reason = "UNKNOWN"
            raise ValueError(f"Model returned empty response. Finish reason: {finish_reason}")
        parsed = json.loads(text)

    normalized = []
    seen = set()
    for raw_skill in parsed or []:
        skill = _normalize_skill_name(str(raw_skill))
        if not skill:
            continue
        key = skill.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "skill": skill,
                "demand": "High",
                "reason": "Broad 2026 market relevance",
            }
        )

    return {"skills": normalized[:7], "role": None, "count": len(normalized[:7])}


def fetch_trending_skills(target_role: str = None):
    """
    Fetch 7 trending skills.
    If target_role is provided, skills are grounded in live RapidAPI job data.
    """
    try:
        cache_key = f"trending_skills_{target_role}" if target_role else "trending_skills"
        cached = _get_fast_cache(cache_key)
        if cached:
            logger.info(f"Cache hit: {cache_key}")
            return {"skills": cached, "role": target_role}

        result = (
            _build_role_trending_from_live_jobs(target_role)
            if target_role
            else _generate_general_trending()
        )

        if result.get("skills"):
            _store_cache_async(cache_key, result["skills"])
        return result
    except Exception as e:
        logger.error(f"Error in fetch_trending_skills: {e}")
        return {"error": str(e), "skills": [], "role": target_role}


TOOL_DEFINITION = {
    "name": "fetch_trending_skills",
    "description": "Fetch top 7 trending tech skills, grounded in live jobs for a target role when provided",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": []
    }
}
