import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

from google import genai
from google.genai import types

from config.settings import settings
from database.firestore_client import get_session, save_session
from utils.logger import get_logger

logger = get_logger(__name__)

_client = None
_client_lock = threading.Lock()
_genai_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="schedule-genai")


def _get_client():
    global _client
    with _client_lock:
        if _client is None:
            _client = genai.Client(api_key=settings.GEMINI_API_KEY)
        return _client


def _generate_topics_now(skill: str, level: str, total_days: int):
    schema = {
        "type": "object",
        "properties": {
            "topics": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": total_days,
                "maxItems": total_days,
            }
        },
        "required": ["topics"],
    }

    response = _get_client().models.generate_content(
        model="gemini-3.6-flash",
        contents=f"""Generate a learning roadmap for {skill} for a {level} learner.

Return exactly {total_days} topics in increasing order of difficulty.
Start with fundamentals and move toward applied practice.
Keep each topic concise and practical.
Return only the topics.""",
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json",
            response_schema=schema,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            http_options=types.HttpOptions(timeout=settings.API_TIMEOUT * 1000),
        ),
    )
    return response.parsed or {}


def generate_schedule(
    session_id: str,
    skill: str,
    level: str,
    mode: str,
    daily_time: int
) -> dict:
    """
    Generate personalized learning schedule.
    """
    try:
        level_key = (level or "Beginner").strip().lower()
        mode_key = (mode or "balanced").strip().lower()
        daily_time = max(15, int(daily_time or 60))

        days_config = {
            "balanced": {"beginner": 7, "intermediate": 5, "advanced": 4},
            "deep_learning": {"beginner": 10, "intermediate": 7, "advanced": 5},
            "fast_track": {"beginner": 3, "intermediate": 2, "advanced": 2},
            "practice_focused": {"beginner": 6, "intermediate": 4, "advanced": 3},
        }
        total_days = days_config.get(mode_key, days_config["balanced"]).get(level_key, 5)

        time_dist = {
            "balanced": {"video": 0.40, "coding": 0.50, "revision": 0.10},
            "deep_learning": {"video": 0.50, "coding": 0.40, "revision": 0.10},
            "fast_track": {"video": 0.30, "coding": 0.60, "revision": 0.10},
            "practice_focused": {"video": 0.25, "coding": 0.65, "revision": 0.10},
        }
        dist = time_dist.get(mode_key, time_dist["balanced"])

        topics = _generate_topics(skill, level, total_days)
        difficulties = _get_difficulty_progression(level, total_days)

        daily_plan = []
        for day in range(1, total_days + 1):
            topic = topics[day - 1] if day <= len(topics) else f"{skill} Practice Day {day}"
            difficulty = difficulties[day - 1] if day <= len(difficulties) else level

            video_time = int(daily_time * dist["video"])
            coding_time = int(daily_time * dist["coding"])
            revision_time = max(5, daily_time - video_time - coding_time)

            daily_plan.append({
                "day": day,
                "topic": topic,
                "difficulty": difficulty,
                "goal": f"Master {topic}",
                "tasks": [
                    {"type": "video", "time": video_time, "description": f"Watch tutorial on {topic}"},
                    {"type": "coding", "time": coding_time, "description": f"Practice {topic} with exercises"},
                    {"type": "revision", "time": revision_time, "description": "Review and reinforce concepts"},
                ],
                "completed": False,
            })

        schedule = {
            "schedule_enabled": True,
            "mode": mode_key,
            "skill": skill,
            "level": level,
            "total_days": total_days,
            "daily_time": daily_time,
            "daily_plan": daily_plan,
            "current_day": 1,
            "progress_percentage": 0,
        }

        logger.info(f"Generated {mode_key} schedule for {skill}: {total_days} days")
        return schedule
    except Exception as e:
        logger.error(f"Error generating schedule: {e}")
        return {"error": str(e)}


def _generate_topics(skill: str, level: str, total_days: int) -> list:
    """Generate topic list using Gemini structured output."""
    try:
        future = _genai_executor.submit(_generate_topics_now, skill, level, total_days)
        try:
            parsed = future.result(timeout=max(settings.API_TIMEOUT + 10, 20))
        except FuturesTimeoutError as timeout_error:
            future.cancel()
            raise TimeoutError(f"Gemini topic generation timed out after {settings.API_TIMEOUT}s") from timeout_error

        topics = [str(topic).strip() for topic in parsed.get("topics", []) if str(topic).strip()]
        while len(topics) < total_days:
            topics.append(f"{skill} Advanced Practice")
        return topics[:total_days]
    except Exception as e:
        logger.error(f"Error generating topics: {e}")
        return [f"{skill} Day {i+1}" for i in range(total_days)]


def _get_difficulty_progression(level: str, total_days: int) -> list:
    """Get difficulty progression for each day."""
    if level.lower() == "beginner":
        difficulties = []
        for i in range(total_days):
            if i < total_days // 3:
                difficulties.append("Easy")
            elif i < 2 * total_days // 3:
                difficulties.append("Medium")
            else:
                difficulties.append("Hard")
        return difficulties
    if level.lower() == "intermediate":
        return ["Medium" if i < total_days // 2 else "Hard" for i in range(total_days)]
    return ["Hard"] * total_days


def get_schedule(session_id: str) -> dict:
    """Get current schedule for a session."""
    try:
        session = get_session(session_id)
        if not session:
            return {"error": "Session not found"}

        schedule = session.get("schedule", {})
        if not schedule:
            return {"schedule_enabled": False}
        return schedule
    except Exception as e:
        logger.error(f"Error getting schedule: {e}")
        return {"error": str(e)}


def update_schedule_progress(session_id: str, day: int, completed: bool = True) -> dict:
    """Update schedule progress - mark day as completed."""
    try:
        session = get_session(session_id)
        if not session:
            return {"error": "Session not found"}

        schedule = session.get("schedule", {})
        if not schedule:
            return {"error": "No schedule found"}

        for plan in schedule.get("daily_plan", []):
            if plan["day"] == day:
                plan["completed"] = completed
                break

        completed_days = sum(1 for p in schedule["daily_plan"] if p.get("completed"))
        total_days = len(schedule["daily_plan"])
        progress = int((completed_days / total_days) * 100) if total_days > 0 else 0

        schedule["progress_percentage"] = progress
        schedule["current_day"] = min(day + 1 if completed else day, total_days)

        save_session(session_id, {"schedule": schedule})
        return {
            "day_completed": day,
            "progress_percentage": progress,
            "current_day": schedule["current_day"],
            "total_days": total_days,
        }
    except Exception as e:
        logger.error(f"Error updating schedule: {e}")
        return {"error": str(e)}


TOOL_DEFINITIONS = [
    {
        "name": "generate_schedule",
        "description": "Generate personalized learning schedule based on mode and available time",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "User session ID"},
                "skill": {"type": "string", "description": "Skill to learn"},
                "level": {"type": "string", "enum": ["Beginner", "Intermediate", "Advanced"], "description": "User level"},
                "mode": {"type": "string", "enum": ["balanced", "deep_learning", "fast_track", "practice_focused"], "description": "Schedule mode"},
                "daily_time": {"type": "integer", "description": "Daily time available in minutes"},
            },
            "required": ["session_id", "skill", "level", "mode", "daily_time"],
        }
    },
    {
        "name": "get_schedule",
        "description": "Get current learning schedule for a session",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "User session ID"}
            },
            "required": ["session_id"]
        }
    },
    {
        "name": "update_schedule_progress",
        "description": "Update schedule progress - mark a day as completed",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "User session ID"},
                "day": {"type": "integer", "description": "Day number to update"},
                "completed": {"type": "boolean", "description": "Whether day is completed"}
            },
            "required": ["session_id", "day"]
        }
    }
]
