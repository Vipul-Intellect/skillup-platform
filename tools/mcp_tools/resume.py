import base64
import concurrent.futures
import json
import mimetypes
import re
import threading
import time
from datetime import datetime, timezone
from io import BytesIO

from PyPDF2 import PdfReader
from google import genai
from google.genai import types
from utils.gemini_client_pool import PooledGemini

from config.settings import settings
from database.firestore_client import (
    get_cache,
    get_role_profile,
    save_role_profile,
)
from utils.logger import get_logger
from utils.validators import sanitize_input
import requests

logger = get_logger(__name__)

HTTP_OPTIONS = types.HttpOptions(timeout=settings.API_TIMEOUT * 1000)
MODEL_ID = "gemini-3.6-flash"
client = None
_role_profile_lock_guard = threading.Lock()
_role_profile_locks = {}
_market_refresh_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="market-profile-refresh",
)
_market_refresh_guard = threading.Lock()
_market_refresh_futures = {}
_cache_read_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="resume-cache-read",
)
_local_role_profile_cache_lock = threading.Lock()
_local_role_profile_cache = {}
_local_trending_fallback_cache_lock = threading.Lock()
_local_trending_fallback_cache = {}

RESUME_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "user_skills": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Technical skills explicitly evidenced by the resume",
        },
        "experience_level": {
            "type": "string",
            "enum": ["Beginner", "Intermediate", "Advanced"],
        },
        "domain": {"type": "string"},
    },
    "required": ["user_skills", "experience_level", "domain"],
    "additionalProperties": False,
}

JOB_SKILLS_SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
    "minItems": 0,
    "maxItems": 10,
}

SKILL_SECTION_HEADERS = (
    "technical skills",
    "skills",
    "tools",
    "technologies",
    "tech stack",
)

STOP_SKILL_TOKENS = {
    "basic",
    "project",
    "projects",
    "summary",
    "certifications",
    "education",
    "experience",
    "in progress",
    "machine learning project",
}

SKILL_ALIASES = {
    "cpp": "C++",
    "c++": "C++",
    "js": "JavaScript",
    "ts": "TypeScript",
    "beautiful soup": "Beautiful Soup",
    "sci kit learn": "Scikit-learn",
    "scikit learn": "Scikit-learn",
    "rag": "RAG",
    "nlp": "NLP",
}


def _get_client():
    global client
    if client is None:
        client = PooledGemini()
    return client


def _generate_json(prompt: str, schema: dict, *, max_output_tokens: int):
    response = _get_client().models.generate_content(
        model=MODEL_ID,
        contents=prompt,
        config=types.GenerateContentConfig(
            max_output_tokens=max_output_tokens,
            response_mime_type="application/json",
            response_json_schema=schema,
            http_options=types.HttpOptions(timeout=settings.API_TIMEOUT * 1000),
        ),
    )

    if getattr(response, "parsed", None) is not None:
        return response.parsed

    return _parse_json_response(response.text)


def _cache_read_timeout_seconds() -> float:
    return min(2.0, max(1.0, settings.API_TIMEOUT / 10))


def _set_local_role_profile_cache(role_key: str, profile: dict, ttl_minutes: int):
    if not profile:
        return

    ttl_seconds = max(60, int(ttl_minutes * 60))
    with _local_role_profile_cache_lock:
        _local_role_profile_cache[role_key] = {
            "value": dict(profile),
            "expires_at": time.time() + ttl_seconds,
        }


def _get_local_role_profile_cache(role_key: str, allow_stale: bool = False):
    with _local_role_profile_cache_lock:
        cached = _local_role_profile_cache.get(role_key)
        if not cached:
            return None

        is_expired = cached["expires_at"] < time.time()
        if is_expired and not allow_stale:
            _local_role_profile_cache.pop(role_key, None)
            return None

        data = dict(cached["value"])
        data["is_stale"] = is_expired
        return data


def _get_fast_role_profile(role_key: str, allow_stale: bool = False):
    local = _get_local_role_profile_cache(role_key, allow_stale=allow_stale)
    if local:
        logger.info(
            f"Local role profile hit: {role_key} ({'stale' if local.get('is_stale') else 'fresh'})"
        )
        return local

    future = _cache_read_executor.submit(get_role_profile, role_key, allow_stale)
    try:
        profile = future.result(timeout=_cache_read_timeout_seconds())
    except Exception as e:
        logger.warning(f"Skipping slow Firestore role profile read for {role_key}: {e}")
        return None

    if profile:
        ttl_minutes = profile.get("ttl_minutes", settings.MARKET_PROFILE_CACHE_TTL_MINUTES)
        _set_local_role_profile_cache(role_key, profile, ttl_minutes)
    return profile


def _set_local_trending_fallback_cache(cache_key: str, skills):
    if not skills:
        return

    ttl_seconds = max(300, int(settings.TRENDING_CACHE_TTL * 3600))
    with _local_trending_fallback_cache_lock:
        _local_trending_fallback_cache[cache_key] = {
            "value": list(skills),
            "expires_at": time.time() + ttl_seconds,
        }


def _get_local_trending_fallback_cache(cache_key: str):
    with _local_trending_fallback_cache_lock:
        cached = _local_trending_fallback_cache.get(cache_key)
        if not cached:
            return None
        if cached["expires_at"] < time.time():
            _local_trending_fallback_cache.pop(cache_key, None)
            return None
        return list(cached["value"])

def analyze_resume(resume_text, session_id):
    try:
        resume_text = sanitize_input(resume_text)
        if not resume_text:
            return {
                "error": "Resume text is empty",
                "user_skills": [],
                "experience_level": "Beginner",
                "domain": "General",
            }

        result = _extract_resume_profile(resume_text)
        logger.info(f"Resume text analyzed for session {session_id}")
        return result
        
    except Exception as e:
        logger.error(f"Error in analyze_resume: {e}")
        return {"error": str(e), "user_skills": [], "experience_level": "Beginner", "domain": "General"}


def analyze_resume_document(file_name, mime_type, file_data_base64, session_id):
    """Analyze a PDF or image resume document."""
    try:
        if not file_data_base64:
            return {"error": "No file data provided"}

        file_bytes = base64.b64decode(file_data_base64)
        normalized_mime_type = _resolve_mime_type(file_name, mime_type)

        if normalized_mime_type == "application/pdf":
            extracted_text = _extract_text_from_pdf(file_bytes)

            if extracted_text:
                result = _extract_resume_profile(extracted_text)
                result["source"] = "pdf_text"
            else:
                return {
                    "error": "Could not extract text from PDF. Upload a text-based PDF or a resume image instead.",
                    "user_skills": [],
                    "experience_level": "Beginner",
                    "domain": "General",
                }

        elif normalized_mime_type in _supported_image_types():
            _validate_image_resume(file_bytes)
            result = _analyze_image_with_gemini(file_bytes, normalized_mime_type)
            result["source"] = "image_vision"

        else:
            return {"error": f"Unsupported resume file type: {normalized_mime_type}"}

        result["file_name"] = file_name
        result["mime_type"] = normalized_mime_type

        logger.info(
            f"Resume document analyzed for session {session_id}: {normalized_mime_type}"
        )
        return result

    except Exception as e:
        logger.error(f"Error in analyze_resume_document: {e}")
        return {
            "error": str(e),
            "user_skills": [],
            "experience_level": "Beginner",
            "domain": "General",
        }

def find_skill_gaps_with_recommendations(
    user_skills,
    target_role,
    session_id,
    force_live_market_profile=False,
):
    try:
        user_skills_set = {s.lower() for s in user_skills}
        market_profile = _get_market_role_profile(
            target_role,
            force_live=force_live_market_profile,
        )

        job_skills = market_profile.get("job_skills", [])
        trending_skills = market_profile.get("trending_skills", [])
        market_profile_source = market_profile.get("source", "unknown")
        warming_sources = {"market_profile_warming", "role_trending_cache_fallback"}

        if not trending_skills and market_profile_source not in warming_sources:
            from tools.mcp_tools.trending import fetch_trending_skills
            trending_result = fetch_trending_skills(target_role=target_role)
            trending_skills = trending_result.get('skills', [])
        trending_set = {s['skill'].lower() for s in trending_skills if s.get("skill")}

        job_skills_set = {s.lower() for s in job_skills}
        skill_gaps = sorted(job_skills_set - user_skills_set)
        recommended = sorted(trending_set.intersection(skill_gaps))

        result = {
            'trending_skills': trending_skills,
            'user_skills': sorted(user_skills_set),
            'skill_gaps': skill_gaps,
            'recommended_skills': recommended,
            'market_profile_source': market_profile_source,
            'market_profile_cached': market_profile.get("cached", False),
            'market_profile_generated_at': market_profile.get("generated_at"),
            'market_profile_ttl_minutes': market_profile.get("ttl_minutes"),
            'market_profile_refresh_queued': market_profile.get("refresh_queued", False),
        }

        if market_profile.get("message"):
            result["message"] = market_profile["message"]

        logger.info(f"Skill gaps computed for session {session_id}")

        return result

    except Exception as e:
        logger.error(f"Error in find_skill_gaps_with_recommendations: {e}")
        return {"error": str(e)}


def _get_market_role_profile(target_role: str, force_live: bool = False):
    normalized_role = (target_role or "software developer").strip()
    role_key = _normalize_cache_key(normalized_role)
    cached_profile = _get_fast_role_profile(role_key, allow_stale=False)
    if cached_profile:
        logger.info(f"Using cached market role profile for {normalized_role}")
        return {
            "job_skills": cached_profile.get("job_skills", []),
            "trending_skills": cached_profile.get("trending_skills", []),
            "source": "firestore_role_profile",
            "cached": True,
            "generated_at": cached_profile.get("generated_at"),
            "ttl_minutes": cached_profile.get("ttl_minutes", settings.MARKET_PROFILE_CACHE_TTL_MINUTES),
        }

    if force_live:
        live_profile = _build_live_market_role_profile(
            normalized_role,
            role_key,
            persist=False,
        )

        if live_profile:
            _persist_role_profile_async(role_key, live_profile)
            logger.info(f"Using forced live market role profile for {normalized_role}")
            return live_profile

    stale_profile = _get_fast_role_profile(role_key, allow_stale=True)
    if stale_profile:
        refresh_queued = schedule_market_profile_refresh(normalized_role, checked_cache=True)
        logger.warning(f"Using stale role profile for {normalized_role} while background refresh runs")
        return {
            "job_skills": stale_profile.get("job_skills", []),
            "trending_skills": stale_profile.get("trending_skills", []),
            "source": "stale_role_profile_fallback",
            "cached": True,
            "generated_at": stale_profile.get("generated_at"),
            "ttl_minutes": stale_profile.get("ttl_minutes", settings.MARKET_PROFILE_CACHE_TTL_MINUTES),
            "refresh_queued": refresh_queued,
            "message": "Using the most recent saved market profile while a background refresh updates live market data.",
        }

    refresh_queued = schedule_market_profile_refresh(normalized_role, checked_cache=True)
    fallback = []
    fallback_skills = []
    source = "market_profile_warming"
    message = "Live job-market profile is warming in the background. Retry shortly for job-based gap analysis."

    return {
        "job_skills": fallback_skills,
        "trending_skills": fallback,
        "source": source,
        "cached": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ttl_minutes": settings.MARKET_PROFILE_CACHE_TTL_MINUTES,
        "refresh_queued": refresh_queued,
        "message": message,
    }


def _build_trending_skill_objects(job_skills, jobs):
    normalized_skills = []
    seen = set()
    for raw_skill in job_skills or []:
        skill = _normalize_skill(raw_skill)
        if not skill:
            continue
        key = skill.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized_skills.append(skill)

    role_texts = []
    for job in jobs or []:
        parts = [job.get("job_title", ""), job.get("job_description", "")]
        highlights = job.get("job_highlights") or {}
        for lines in highlights.values():
            if isinstance(lines, list):
                parts.extend(str(line) for line in lines)
        role_texts.append(" ".join(parts).lower())

    trending = []
    for idx, skill in enumerate(normalized_skills[:7]):
        skill_lower = skill.lower()
        mentions = sum(1 for text in role_texts if skill_lower in text)

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

        trending.append(
            {
                "skill": skill,
                "demand": demand,
                "reason": reason,
            }
        )

    return trending


def _build_live_market_role_profile(target_role: str, role_key: str, persist: bool = True):
    jobs = _fetch_jobs(target_role)
    if not jobs:
        return None

    job_skills = _extract_job_skills(jobs)
    if not job_skills:
        return None

    trending_skills = _build_trending_skill_objects(job_skills, jobs)
    generated_at = datetime.now(timezone.utc).isoformat()
    profile = {
        "role": target_role,
        "job_skills": sorted(
            {_normalize_skill(skill) for skill in job_skills if _normalize_skill(skill)}
        ),
        "trending_skills": trending_skills,
        "job_count": len(jobs),
        "generated_at": generated_at,
        "ttl_minutes": settings.MARKET_PROFILE_CACHE_TTL_MINUTES,
        "source": "live_jobs",
    }
    _set_local_role_profile_cache(
        role_key,
        profile,
        settings.MARKET_PROFILE_CACHE_TTL_MINUTES,
    )
    if persist:
        save_role_profile(role_key, profile, settings.MARKET_PROFILE_CACHE_TTL_MINUTES)
    return {
        "job_skills": profile["job_skills"],
        "trending_skills": profile["trending_skills"],
        "source": "live_jobs",
        "cached": False,
        "generated_at": generated_at,
        "ttl_minutes": settings.MARKET_PROFILE_CACHE_TTL_MINUTES,
    }


def _persist_role_profile_async(role_key: str, live_profile: dict):
    profile = {
        "role": role_key,
        "job_skills": live_profile.get("job_skills", []),
        "trending_skills": live_profile.get("trending_skills", []),
        "job_count": live_profile.get("job_count"),
        "generated_at": live_profile.get("generated_at"),
        "ttl_minutes": live_profile.get("ttl_minutes", settings.MARKET_PROFILE_CACHE_TTL_MINUTES),
        "source": live_profile.get("source", "live_jobs"),
    }

    def _persist():
        try:
            save_role_profile(role_key, profile, settings.MARKET_PROFILE_CACHE_TTL_MINUTES)
        except Exception as e:
            logger.warning(f"Async role profile persist failed for {role_key}: {e}")

    _market_refresh_executor.submit(_persist)


def _refresh_market_role_profile(target_role: str, role_key: str):
    return _build_live_market_role_profile(target_role, role_key, persist=True)


def schedule_market_profile_refresh(target_role: str, force: bool = False, checked_cache: bool = False):
    normalized_role = (target_role or "software developer").strip()
    role_key = _normalize_cache_key(normalized_role)

    if not force and not checked_cache:
        cached_profile = _get_fast_role_profile(role_key, allow_stale=False)
        if cached_profile:
            return False

    with _market_refresh_guard:
        existing_future = _market_refresh_futures.get(role_key)
        if existing_future and not existing_future.done():
            return False

        future = _market_refresh_executor.submit(
            _refresh_market_role_profile,
            normalized_role,
            role_key,
        )
        _market_refresh_futures[role_key] = future

    def _cleanup(completed_future):
        with _market_refresh_guard:
            _market_refresh_futures.pop(role_key, None)
        exc = completed_future.exception()
        if exc:
            logger.warning(f"Background market profile refresh failed for {normalized_role}: {exc}")
        else:
            logger.info(f"Background market profile refresh completed for {normalized_role}")

    future.add_done_callback(_cleanup)
    logger.info(f"Queued background market profile refresh for {normalized_role}")
    return True


def prewarm_market_profiles(roles=None):
    queued_roles = []
    for role in (roles or settings.HOT_MARKET_ROLES):
        if schedule_market_profile_refresh(role):
            queued_roles.append(role)
    return queued_roles


def _get_cached_trending_skills(target_role: str):
    cache_keys = []
    if target_role:
        cache_keys.append(f"trending_skills_{target_role}")
    cache_keys.append("trending_skills")

    for cache_key in cache_keys:
        cached = _get_local_trending_fallback_cache(cache_key)
        if cached:
            logger.info(f"Using local cached trending fallback: {cache_key}")
            return cached

        future = _cache_read_executor.submit(get_cache, cache_key)
        try:
            cached = future.result(timeout=_cache_read_timeout_seconds())
        except Exception as e:
            logger.warning(f"Skipping slow Firestore trending fallback read for {cache_key}: {e}")
            cached = None
        if cached:
            logger.info(f"Using cached trending skills fallback: {cache_key}")
            _set_local_trending_fallback_cache(cache_key, cached)
            return cached
    return []

def _fetch_jobs(target_role):
    try:
        url = "https://jsearch.p.rapidapi.com/search"
        headers = {
            "X-RapidAPI-Key": settings.RAPIDAPI_KEY,
            "X-RapidAPI-Host": settings.RAPIDAPI_JSEARCH_HOST
        }
        params = {
            "query": target_role,
            "num_pages": "1",
            "date_posted": "month"
        }
        
        response = requests.get(url, headers=headers, params=params, timeout=settings.API_TIMEOUT)
        
        if response.status_code == 200:
            data = response.json()
            jobs = data.get('data', [])[:3]
            logger.info(f"Fetched {len(jobs)} jobs for {target_role}")
            return jobs
        else:
            logger.error(f"Job API error: {response.status_code}")
            return []
            
    except Exception as e:
        logger.error(f"Error fetching jobs: {e}")
        return []

def _extract_job_skills(jobs):
    try:
        job_signals = "\n\n".join([
            _build_job_signal(job, index)
            for index, job in enumerate(jobs[:3], start=1)
        ])

        prompt = f"""Extract the top 10 most common technical skills mentioned across these current job-market snippets.

Use only concrete technical skills that appear in the snippets. Ignore soft skills, degrees, years of experience, and generic responsibilities.

Jobs:
{job_signals}

Return ONLY a JSON array of skill names (no markdown, no code blocks):
["Python", "Docker", "Kubernetes", ...]"""

        skills = _generate_json(prompt, JOB_SKILLS_SCHEMA, max_output_tokens=2048)
        logger.info(f"Extracted {len(skills)} skills from job-market snippets")
        return skills
        
    except Exception as e:
        logger.error(f"Error extracting job skills: {e}")
        return []


def _build_job_signal(job, index: int):
    highlights = job.get("job_highlights") or {}
    sections = []
    for section_name, section_lines in highlights.items():
        if not isinstance(section_lines, list) or not section_lines:
            continue
        if any(keyword in section_name.lower() for keyword in ("qualification", "requirement", "skill")):
            trimmed_lines = [str(line).strip() for line in section_lines[:4] if str(line).strip()]
            if trimmed_lines:
                sections.append(f"{section_name}: " + " | ".join(trimmed_lines))

    if not sections:
        description = (job.get("job_description") or "")[:240]
        if description:
            sections.append(f"Description: {description}")

    return (
        f"Job {index}: {job.get('job_title', '')}\n"
        + "\n".join(sections)
    )


def _iter_job_skill_lines(job):
    description = job.get("job_description") or ""
    if description:
        for line in re.split(r"[\r\n]+", description[:1200]):
            cleaned = line.strip()
            if cleaned:
                yield cleaned


def _normalize_cache_key(value: str):
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "general"


def _get_role_profile_lock(role_key: str):
    with _role_profile_lock_guard:
        if role_key not in _role_profile_locks:
            _role_profile_locks[role_key] = threading.Lock()
        return _role_profile_locks[role_key]


def _analyze_resume_text(resume_text: str):
    prompt = f"""Analyze this resume and extract technical skills.

Resume:
{resume_text}

Return ONLY valid JSON (no markdown, no code blocks):
{{
  "user_skills": ["skill1", "skill2", ...],
  "experience_level": "Beginner/Intermediate/Advanced",
  "domain": "Backend/Frontend/DevOps/ML/etc"
}}"""

    return _generate_json(prompt, RESUME_RESPONSE_SCHEMA, max_output_tokens=2048)


def _extract_resume_profile(resume_text: str):
    try:
        gemini_result = _analyze_resume_text(resume_text)
        normalized_result = _normalize_resume_result(gemini_result)

        if normalized_result["user_skills"]:
            normalized_result["analysis_mode"] = "gemini"
            return normalized_result

        logger.warning("Gemini resume extraction returned no skills, using fallback parser")
    except Exception as e:
        logger.warning(f"Gemini resume extraction failed, using fallback parser: {e}")

    heuristic_result = _extract_resume_profile_heuristic(resume_text)
    heuristic_result["analysis_mode"] = "heuristic_fallback"
    return heuristic_result


def _extract_resume_profile_heuristic(resume_text: str):
    normalized_text = resume_text.lower()
    detected_skills = _extract_skills_from_sections(resume_text)

    detected_skills = sorted(set(detected_skills))

    return {
        "user_skills": detected_skills,
        "experience_level": _infer_experience_level(normalized_text),
        "domain": _infer_domain(detected_skills),
    }


def _normalize_resume_result(result: dict):
    normalized_skills = []
    for skill in result.get("user_skills", []):
        normalized_skill = _normalize_skill(str(skill))
        if normalized_skill:
            normalized_skills.append(normalized_skill)

    normalized_skills = sorted(set(normalized_skills))

    experience_level = str(result.get("experience_level", "Beginner")).strip().title()
    if experience_level not in {"Beginner", "Intermediate", "Advanced"}:
        experience_level = "Beginner"

    domain = str(result.get("domain", "General")).strip() or "General"

    return {
        "user_skills": normalized_skills,
        "experience_level": experience_level,
        "domain": domain,
    }


def _infer_experience_level(normalized_text: str):
    years = re.findall(r"(\d+)\+?\s+years?", normalized_text)
    max_years = max((int(year) for year in years), default=0)

    if "senior" in normalized_text or max_years >= 5:
        return "Advanced"
    if "mid-level" in normalized_text or "intermediate" in normalized_text or max_years >= 2:
        return "Intermediate"
    return "Beginner"


def _infer_domain(skills):
    lowered = {skill.lower() for skill in skills}

    if lowered & {"tensorflow", "pytorch", "machine learning", "deep learning", "mlops"}:
        return "ML/AI"
    if lowered & {"docker", "kubernetes", "terraform", "jenkins", "aws", "azure", "gcp"}:
        return "Backend/DevOps"
    if lowered & {"react", "next.js", "html", "css", "javascript", "typescript"}:
        return "Frontend"
    if lowered & {"flask", "django", "fastapi", "node.js", "express", "sql", "postgresql"}:
        return "Backend"
    return "General"


def _extract_skills_from_sections(resume_text: str):
    lines = [line.strip() for line in resume_text.splitlines()]
    collected = []
    in_skill_section = False

    for line in lines:
        normalized_line = line.lower().strip()
        if not normalized_line:
            continue

        if _is_section_header(normalized_line):
            in_skill_section = normalized_line in SKILL_SECTION_HEADERS
            continue

        if in_skill_section and _looks_like_new_section(line):
            in_skill_section = False

        if not in_skill_section and ":" not in line:
            continue

        if in_skill_section or _looks_like_skill_row(line):
            collected.extend(_extract_skills_from_line(line))

    return [_normalize_skill(skill) for skill in collected if _normalize_skill(skill)]


def _is_section_header(normalized_line: str):
    collapsed = re.sub(r"[^a-z ]+", "", normalized_line).strip()
    return (
        collapsed in SKILL_SECTION_HEADERS
        or (collapsed.isupper() and len(collapsed.split()) <= 4)
    )


def _looks_like_new_section(line: str):
    stripped = line.strip()
    letters_only = re.sub(r"[^A-Za-z ]+", "", stripped).strip()
    return (
        stripped == stripped.upper()
        or (letters_only.isupper() and len(letters_only.split()) <= 4)
    )


def _looks_like_skill_row(line: str):
    lowered = line.lower()
    return any(keyword in lowered for keyword in (":", "framework", "tools", "language", "platform"))


def _extract_skills_from_line(line: str):
    candidates = []

    if ":" in line:
        line = line.split(":", 1)[1]

    normalized = line.replace("|", ",").replace(";", ",")
    parts = [part.strip() for part in normalized.split(",")]

    for part in parts:
        cleaned = re.sub(r"\(.*?\)", "", part).strip(" -\t")
        if not cleaned:
            continue

        subparts = [token.strip() for token in re.split(r"/", cleaned) if token.strip()]
        for token in subparts:
            if _is_valid_skill_token(token):
                candidates.append(token)

    return candidates


def _is_valid_skill_token(token: str):
    lowered = token.lower()
    if lowered in STOP_SKILL_TOKENS:
        return False
    if len(token) < 2:
        return False
    if re.fullmatch(r"\d+", token):
        return False
    return True


def _normalize_skill(skill: str):
    cleaned = re.sub(r"\s+", " ", skill).strip(" ,.-")
    lowered = cleaned.lower()

    if lowered in STOP_SKILL_TOKENS:
        return ""

    aliased = SKILL_ALIASES.get(lowered, cleaned)
    if aliased.isupper() and len(aliased) > 4:
        return aliased.title()
    return aliased


def _analyze_image_with_gemini(file_bytes: bytes, mime_type: str):
    prompt = """Analyze this resume image and extract technical skills.

Return ONLY valid JSON (no markdown, no code blocks):
{
  "user_skills": ["skill1", "skill2", ...],
  "experience_level": "Beginner/Intermediate/Advanced",
  "domain": "Backend/Frontend/DevOps/ML/etc"
}"""

    response = _get_client().models.generate_content(
        model=MODEL_ID,
        contents=[
            types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
            prompt,
        ],
        config=types.GenerateContentConfig(
            max_output_tokens=2048,
            response_mime_type="application/json",
            response_json_schema=RESUME_RESPONSE_SCHEMA,
            http_options=types.HttpOptions(timeout=settings.API_TIMEOUT * 1000),
        ),
    )

    if getattr(response, "parsed", None) is not None:
        return response.parsed

    return _parse_json_response(response.text)

def _extract_text_from_pdf(file_bytes: bytes) -> str:
    try:
        reader = PdfReader(BytesIO(file_bytes))
        pages = []
        for page in reader.pages:
            text = page.extract_text() or ""
            pages.append(text)

        extracted_text = "\n".join(pages).strip()
        return sanitize_input(extracted_text)
    except Exception as e:
        logger.warning(f"PDF text extraction failed: {e}")
        return ""


def _parse_json_response(text: str):
    cleaned = (text or "").strip()
    cleaned = cleaned.replace("```json", "").replace("```", "").strip()
    if not cleaned:
        raise ValueError("Model returned empty response after cleaning.")
    if not cleaned:
        raise ValueError("Model returned empty response after cleaning.")
    return json.loads(cleaned)


def _supported_image_types():
    return {
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/webp",
    }


def _validate_image_resume(file_bytes: bytes):
    if len(file_bytes) < 5 * 1024:
        raise ValueError(
            "Resume image is too small to analyze reliably. Upload a clearer screenshot or photo."
        )


def _resolve_mime_type(file_name: str, mime_type: str):
    normalized = (mime_type or "").strip().lower()
    if normalized:
        if normalized == "image/jpg":
            return "image/jpeg"
        return normalized

    guessed_type, _ = mimetypes.guess_type(file_name or "")
    if guessed_type == "image/jpg":
        return "image/jpeg"
    return guessed_type or "application/octet-stream"

TOOL_DEFINITIONS = [
    {
        "name": "analyze_resume",
        "description": "Analyze resume text and extract technical skills, experience level, and domain",
        "parameters": {
            "type": "object",
            "properties": {
                "resume_text": {"type": "string", "description": "The resume text content"},
                "session_id": {"type": "string", "description": "User session ID"}
            },
            "required": ["resume_text", "session_id"]
        }
    },
    {
        "name": "analyze_resume_document",
        "description": "Analyze a resume PDF or image by extracting text or using Gemini vision",
        "parameters": {
            "type": "object",
            "properties": {
                "file_name": {"type": "string", "description": "Original file name"},
                "mime_type": {"type": "string", "description": "Uploaded file MIME type"},
                "file_data_base64": {"type": "string", "description": "Base64-encoded file bytes"},
                "session_id": {"type": "string", "description": "User session ID"}
            },
            "required": ["file_name", "mime_type", "file_data_base64", "session_id"]
        }
    },
    {
        "name": "find_skill_gaps_with_recommendations",
        "description": "Find skill gaps by comparing user skills with job requirements and recommend trending skills",
        "parameters": {
            "type": "object",
            "properties": {
                "user_skills": {"type": "array", "items": {"type": "string"}, "description": "List of user's current skills"},
                "target_role": {"type": "string", "description": "Target job role"},
                "session_id": {"type": "string", "description": "User session ID"}
            },
            "required": ["user_skills", "target_role", "session_id"]
        }
    }
]
