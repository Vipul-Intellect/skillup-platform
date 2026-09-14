import json
import re
import threading
from urllib.parse import quote_plus
from collections import OrderedDict

import requests
from google import genai
from google.genai import types

from config.settings import settings
from utils.logger import get_logger
from utils.gemini_client_pool import PooledGemini

logger = get_logger(__name__)

TOPIC_CACHE_PREFIX = "learning_topics"
VIDEO_CACHE_PREFIX = "learning_videos"
VIDEO_TARGET_TOTAL = 12
CURATED_TARGET = 6
LIVE_TARGET = 6
YOUTUBE_REQUEST_TIMEOUT = max(3, min(int(settings.API_TIMEOUT or 30), 5))

_http = requests.Session()
_topic_cache_lock = threading.Lock()
_video_cache_lock = threading.Lock()
_topic_memory_cache = OrderedDict()
_video_memory_cache = OrderedDict()
_MEMORY_CACHE_LIMIT = 64
_gemini_client = None


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = PooledGemini()
    return _gemini_client


def _memory_cache_get(cache: OrderedDict, key: str, lock: threading.Lock):
    with lock:
        value = cache.get(key)
        if value is None:
            return None
        cache.move_to_end(key)
        return value


def _memory_cache_set(cache: OrderedDict, key: str, value, lock: threading.Lock):
    with lock:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > _MEMORY_CACHE_LIMIT:
            cache.popitem(last=False)


def _normalize_topic(value: str) -> str:
    topic = (value or "").strip()
    topic = re.sub(r"\s+", " ", topic)
    return topic


def _topic_cache_key(skill: str, level: str, exclude_topics: list[str], count: int) -> str:
    excluded = "|".join(sorted(_normalize_topic(topic).lower() for topic in exclude_topics if topic))
    return f"{TOPIC_CACHE_PREFIX}:{skill.strip().lower()}:{level.strip().lower()}:{count}:{excluded}"


def _video_cache_key(skill: str, level: str, topic: str, preferred_duration: str, max_results: int) -> str:
    return (
        f"{VIDEO_CACHE_PREFIX}:"
        f"{skill.strip().lower()}:"
        f"{level.strip().lower()}:"
        f"{_normalize_topic(topic).lower()}:"
        f"{preferred_duration.strip().lower()}:"
        f"{max_results}"
    )


def recommend_topics(skill: str, level: str, count: int = 3, exclude_topics: list | None = None) -> dict:
    """
    Recommend 2-3 learnable topics for the selected skill and level.
    Users can send already-known topics in exclude_topics to get replacements.
    """
    try:
        exclude_topics = exclude_topics or []
        count = max(1, min(int(count or 3), 3))
        cache_key = _topic_cache_key(skill, level, exclude_topics, count)

        cached = _memory_cache_get(_topic_memory_cache, cache_key, _topic_cache_lock)
        if cached:
            return cached

        client = _get_gemini_client()
        excluded_clause = ", ".join(_normalize_topic(topic) for topic in exclude_topics if topic) or "None"

        schema = {
            "type": "object",
            "properties": {
                "topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": count,
                    "maxItems": count,
                }
            },
            "required": ["topics"],
        }

        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=f"""Suggest exactly {count} high-value learning topics for the skill "{skill}" for a {level} learner.

Constraints:
- Topics must be practical and useful for learning progression.
- Topics should be concise, human-readable, and distinct.
- Avoid any excluded topics.
- Excluded topics: {excluded_clause}
- Return only topics, no explanations.""",
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=schema,
                http_options=types.HttpOptions(timeout=settings.API_TIMEOUT * 1000),
            ),
        )

        parsed = response.parsed or {}
        topics = [
            _normalize_topic(topic)
            for topic in parsed.get("topics", [])
            if _normalize_topic(topic)
        ]

        seen = set()
        filtered = []
        excluded = {_normalize_topic(topic).lower() for topic in exclude_topics}
        for topic in topics:
            lowered = topic.lower()
            if lowered in excluded or lowered in seen:
                continue
            filtered.append(topic)
            seen.add(lowered)
            if len(filtered) == count:
                break

        if len(filtered) < count:
            raise ValueError("Topic recommendation returned insufficient topics")

        result = {
            "skill": skill,
            "level": level,
            "topics": filtered,
            "excluded_topics": [_normalize_topic(topic) for topic in exclude_topics if _normalize_topic(topic)],
            "total": len(filtered),
        }
        _memory_cache_set(_topic_memory_cache, cache_key, result, _topic_cache_lock)
        return result
    except Exception as e:
        logger.error(f"Error recommending topics: {e}")
        return {"error": str(e), "topics": []}


def search_videos(
    skill: str,
    level: str,
    max_results: int = VIDEO_TARGET_TOTAL,
    topic: str = "",
    preferred_duration: str = "",
) -> dict:
    """
    Search YouTube for videos using 6 curated-channel results plus 6 live-search results.
    """
    try:
        desired_total = max(1, min(int(max_results or VIDEO_TARGET_TOTAL), VIDEO_TARGET_TOTAL))
        target_topic = _normalize_topic(topic)
        preferred_duration = (preferred_duration or "").strip()
        cache_key = _video_cache_key(skill, level, target_topic, preferred_duration, desired_total)

        cached = _memory_cache_get(_video_memory_cache, cache_key, _video_cache_lock)
        if cached:
            return cached

        query = _build_query(skill, level, target_topic)
        search_duration = _youtube_duration_filter(preferred_duration)

        curated_raw = _search_curated_channels(query, search_duration)
        live_raw = _search_global(
            skill=skill,
            level=level,
            topic=target_topic,
            duration_filter=search_duration,
            limit=max(LIVE_TARGET, desired_total),
            exclude_video_ids={item["video_id"] for item in curated_raw},
        )

        curated_videos = _hydrate_video_records(curated_raw, source="curated", preferred_duration=preferred_duration)
        live_videos = _hydrate_video_records(live_raw, source="live", preferred_duration=preferred_duration)

        all_candidates = []
        seen_vids = set()
        for v in curated_videos + live_videos:
            if v["video_id"] not in seen_vids:
                seen_vids.add(v["video_id"])
                all_candidates.append(v)

        fallback_used = False
        if not all_candidates:
            final_videos = _build_fallback_videos(
                skill=skill,
                level=level,
                topic=target_topic,
                preferred_duration=preferred_duration,
                max_results=desired_total,
            )
            fallback_used = True
        else:
            evaluated_candidates = _evaluate_and_rank_videos(
                candidates=all_candidates,
                skill=skill,
                level=level,
                topic=target_topic,
            )
            final_videos = evaluated_candidates[:desired_total]

        result = {
            "skill": skill,
            "level": level,
            "topic": target_topic or skill,
            "preferred_duration": preferred_duration or None,
            "videos": final_videos,
            "total": len(final_videos),
            "fallback_used": fallback_used,
            "counts": {
                "curated": len([video for video in final_videos if video["source"] == "curated"]),
                "live": len([video for video in final_videos if video["source"] == "live"]),
            },
            "target_mix": {
                "curated": min(CURATED_TARGET, desired_total),
                "live": min(LIVE_TARGET, max(0, desired_total - min(CURATED_TARGET, desired_total))),
            },
        }

        _memory_cache_set(_video_memory_cache, cache_key, result, _video_cache_lock)
        logger.info(
            f"Found {len(final_videos)} videos for {skill}/{target_topic or skill} ({level}) "
            f"with {result['counts']['curated']} curated and {result['counts']['live']} live"
        )
        return result
    except Exception as e:
        logger.error(f"Error searching videos: {e}")
        return {"error": str(e), "videos": []}


def _build_fallback_videos(
    skill: str,
    level: str,
    topic: str,
    preferred_duration: str,
    max_results: int,
) -> list[dict]:
    base_topic = _normalize_topic(topic) or _normalize_topic(skill)
    queries = [
        f"{skill} {base_topic} tutorial {level}",
        f"{base_topic} hands on tutorial {level}",
        f"{skill} {base_topic} practice project",
        f"{base_topic} interview prep {level}",
    ]

    deduped_queries = []
    seen = set()
    for query in queries:
        normalized = " ".join(query.split()).strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped_queries.append(normalized)

    fallback = []
    for query in deduped_queries[: max(1, min(max_results, 4))]:
        fallback.append(
            {
                "video_id": "",
                "title": f"YouTube search: {query}",
                "url": f"https://www.youtube.com/results?search_query={quote_plus(query)}",
                "thumbnail": "",
                "channel": "YouTube Search",
                "views": 0,
                "likes": 0,
                "duration": preferred_duration or "Flexible",
                "duration_seconds": 0,
                "published_at": None,
                "source": "fallback",
                "trusted_channel": False,
                "relevance_score": 0,
                "duration_match_score": 0,
            }
        )
    return fallback


def _safe_score(val) -> int:
    try:
        if val is None:
            return 0
        v = int(float(val))
        return max(0, min(10, v))
    except (ValueError, TypeError):
        return 0

def _evaluate_and_rank_videos(
    candidates: list[dict], skill: str, level: str, topic: str
) -> list[dict]:
    if not candidates:
        return []

    is_semantic_fallback = False
    
    payload = []
    for c in candidates:
        payload.append({
            "video_id": c["video_id"],
            "title": c["title"],
            "channel": c["channel"],
            "duration": c["duration"],
            "published_at": c.get("published_at"),
            "description": c.get("description", "")[:2000]
        })
        
    schema = {
        "type": "OBJECT",
        "properties": {
            "evaluations": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "video_id": {"type": "STRING"},
                        "topic_match_score": {"type": "INTEGER", "minimum": 0, "maximum": 10},
                        "concept_coverage_score": {"type": "INTEGER", "minimum": 0, "maximum": 10},
                        "learner_level_fit_score": {"type": "INTEGER", "minimum": 0, "maximum": 10},
                        "content_focus_score": {"type": "INTEGER", "minimum": 0, "maximum": 10},
                        "source_quality_score": {"type": "INTEGER", "minimum": 0, "maximum": 10},
                        "technical_freshness_score": {"type": "INTEGER", "minimum": 0, "maximum": 10},
                        "concepts_covered": {"type": "ARRAY", "items": {"type": "STRING"}, "maxItems": 5},
                        "why_recommended": {"type": "ARRAY", "items": {"type": "STRING"}, "maxItems": 3},
                        "short_description": {"type": "STRING", "description": "1-2 lines"}
                    },
                    "required": [
                        "video_id", "topic_match_score", "concept_coverage_score", 
                        "learner_level_fit_score", "content_focus_score", "source_quality_score",
                        "technical_freshness_score", "concepts_covered", "why_recommended", "short_description"
                    ]
                }
            }
        },
        "required": ["evaluations"]
    }
    
    prompt = f"""Evaluate these YouTube video candidates for a learner based ONLY on the provided metadata.
DO NOT invent information, concepts, technologies, or claims not present in the metadata.
If the metadata does not provide enough evidence, be conservative.
Assess technical freshness conservatively based on the title, description, and publication date; date alone does not prove accuracy.

Skill: {skill}
Topic: {topic or skill}
Learner Level: {level}

Candidates:
{json.dumps(payload, indent=2)}

Return structured evaluations scoring each out of 10."""

    evals = {}
    try:
        client = _get_gemini_client()
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=schema,
            ),
        )
        text = response.text
        if not text:
            try:
                finish_reason = response.candidates[0].finish_reason
            except (IndexError, AttributeError):
                finish_reason = "UNKNOWN"
            raise ValueError(f"Model returned empty response. Finish reason: {finish_reason}")
        data = json.loads(text)
        evals = {e["video_id"]: e for e in data.get("evaluations", [])}
    except Exception as e:
        logger.warning(f"Semantic evaluation failed, falling back to deterministic: {e}")
        is_semantic_fallback = True

    for c in candidates:
        vid = c["video_id"]
        ev = evals.get(vid)
        
        if ev and not is_semantic_fallback:
            views = c.get("views", 0)
            if views > 1000000:
                eng_bonus = 4
            elif views > 100000:
                eng_bonus = 3
            elif views > 10000:
                eng_bonus = 2
            elif views > 1000:
                eng_bonus = 1
            else:
                eng_bonus = 0
                
            base_score = (
                _safe_score(ev.get("topic_match_score")) * 2.4 +
                _safe_score(ev.get("concept_coverage_score")) * 2.4 +
                _safe_score(ev.get("learner_level_fit_score")) * 1.6 +
                _safe_score(ev.get("content_focus_score")) * 1.4 +
                _safe_score(ev.get("source_quality_score")) * 1.0 +
                _safe_score(ev.get("technical_freshness_score")) * 0.8
            )
            c["learning_fit_score"] = int(min(100, base_score + eng_bonus))
            c["concepts_covered"] = ev.get("concepts_covered", [])
            c["why_recommended"] = ev.get("why_recommended", [])
            c["short_description"] = ev.get("short_description", "")
            c["is_semantic_fallback"] = False
        else:
            rel_score = c.get("relevance_score", 0)
            dur_score = c.get("duration_match_score", 0)
            raw_fallback = rel_score + dur_score
            c["learning_fit_score"] = int(min(100, (raw_fallback / 10.0) * 100))
            c["concepts_covered"] = []
            c["why_recommended"] = []
            c["short_description"] = ""
            c["is_semantic_fallback"] = True

    candidates.sort(key=lambda x: x["learning_fit_score"], reverse=True)
    return candidates


def _build_query(skill: str, level: str, topic: str) -> str:
    base = _normalize_topic(topic) or _normalize_topic(skill)
    return f"{skill} {base} tutorial {level}".strip()


def _youtube_duration_filter(preferred_duration: str) -> str | None:
    normalized = (preferred_duration or "").strip().lower()
    if not normalized:
        return None
    if normalized == "10 min":
        return "medium"
    if normalized in {"40 min", "60 min", "2 hours"}:
        return "long"
    return None


def _search_curated_channels(query: str, duration_filter: str | None) -> list[dict]:
    videos = []
    for channel_id in settings.MASTER_CHANNELS:
        videos.extend(
            _search_api_safe(
                query,
                max_results=2,
                channel_id=channel_id,
                duration_filter=duration_filter,
                order="relevance",
            )
        )
        if len(videos) >= CURATED_TARGET * 2:
            break
    return videos


def _search_global(
    skill: str,
    level: str,
    topic: str,
    duration_filter: str | None,
    limit: int,
    exclude_video_ids: set[str] | None = None,
) -> list[dict]:
    exclude_video_ids = exclude_video_ids or set()
    queries = _build_live_queries(skill, level, topic)
    results = []
    seen_ids = set(exclude_video_ids)

    for query in queries:
        batch = _search_api_safe(
            query,
            max_results=max(limit, LIVE_TARGET),
            channel_id=None,
            duration_filter=duration_filter,
            order="relevance",
        )
        for item in batch:
            video_id = item["video_id"]
            channel_id = item.get("channel_id")
            if video_id in seen_ids:
                continue
            if channel_id in settings.MASTER_CHANNELS:
                continue
            seen_ids.add(video_id)
            results.append(item)
            if len(results) >= limit:
                return results

    return results


def _build_live_queries(skill: str, level: str, topic: str) -> list[str]:
    base_topic = _normalize_topic(topic) or _normalize_topic(skill)
    queries = [
        _build_query(skill, level, base_topic),
        f"{skill} {base_topic} explained {level}".strip(),
        f"{skill} {base_topic} practice {level}".strip(),
    ]

    deduped = []
    seen = set()
    for query in queries:
        normalized = " ".join(query.split()).strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(normalized)
    return deduped


def _search_api(
    query: str,
    max_results: int,
    channel_id: str | None,
    duration_filter: str | None,
    order: str,
) -> list[dict]:
    params = {
        "key": settings.YOUTUBE_API_KEY,
        "q": query,
        "part": "snippet",
        "type": "video",
        "maxResults": max_results,
        "order": order,
        "videoEmbeddable": "true",
    }
    if channel_id:
        params["channelId"] = channel_id
    if duration_filter:
        params["videoDuration"] = duration_filter

    response = _http.get(
        "https://www.googleapis.com/youtube/v3/search",
        params=params,
        timeout=YOUTUBE_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()

    results = []
    for item in data.get("items", []):
        video_id = item.get("id", {}).get("videoId")
        snippet = item.get("snippet", {})
        if not video_id:
            continue
        results.append(
            {
                "video_id": video_id,
                "title": snippet.get("title", ""),
                "channel": snippet.get("channelTitle", ""),
                "channel_id": snippet.get("channelId", ""),
                "search_query": query,
                "published_at": snippet.get("publishedAt"),
                "thumbnail": (snippet.get("thumbnails", {}).get("medium") or {}).get("url"),
                "description": snippet.get("description", ""),
            }
        )
    return results


def _search_api_safe(
    query: str,
    max_results: int,
    channel_id: str | None,
    duration_filter: str | None,
    order: str,
) -> list[dict]:
    try:
        return _search_api(query, max_results, channel_id, duration_filter, order)
    except Exception as e:
        scope = channel_id or "global"
        logger.warning(f"YouTube search skipped for {scope}: {e}")
        return []


def _hydrate_video_records(search_results: list[dict], source: str, preferred_duration: str) -> list[dict]:
    if not search_results:
        return []

    deduped = OrderedDict()
    for item in search_results:
        video_id = item["video_id"]
        if video_id not in deduped:
            deduped[video_id] = item

    stats_map = _fetch_video_details(list(deduped.keys()))
    hydrated = []
    for video_id, item in deduped.items():
        details = stats_map.get(video_id, {})
        duration_seconds = details.get("duration_seconds", 0)
        relevance_score = _relevance_score(item["title"], item.get("channel", ""), item.get("search_query", ""))
        hydrated.append(
            {
                "video_id": video_id,
                "title": item["title"],
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "thumbnail": item.get("thumbnail"),
                "channel": item.get("channel", ""),
                "views": details.get("view_count", 0),
                "likes": details.get("like_count", 0),
                "duration": details.get("duration_text"),
                "duration_seconds": duration_seconds,
                "published_at": item.get("published_at"),
                "description": item.get("description", ""),
                "source": source,
                "trusted_channel": source == "curated",
                "relevance_score": relevance_score,
                "duration_match_score": _duration_match_score(duration_seconds, preferred_duration),
            }
        )

    hydrated.sort(
        key=lambda video: (
            video["relevance_score"],
            video["duration_match_score"],
            video["views"],
            video["likes"],
        ),
        reverse=True,
    )
    return hydrated


def _fetch_video_details(video_ids: list[str]) -> dict:
    if not video_ids:
        return {}

    try:
        response = _http.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={
                "key": settings.YOUTUBE_API_KEY,
                "id": ",".join(video_ids),
                "part": "contentDetails,statistics",
            },
            timeout=YOUTUBE_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.warning(f"YouTube detail lookup skipped: {e}")
        data = {"items": []}

    details = {}
    for item in data.get("items", []):
        details[item["id"]] = {
            "view_count": int((item.get("statistics") or {}).get("viewCount", 0)),
            "like_count": int((item.get("statistics") or {}).get("likeCount", 0)),
            "duration_text": _iso8601_to_text((item.get("contentDetails") or {}).get("duration", "PT0S")),
            "duration_seconds": _iso8601_to_seconds((item.get("contentDetails") or {}).get("duration", "PT0S")),
        }
    return details


def _iso8601_to_seconds(value: str) -> int:
    pattern = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")
    match = pattern.fullmatch(value or "PT0S")
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def _iso8601_to_text(value: str) -> str:
    total_seconds = _iso8601_to_seconds(value)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds and not hours:
        parts.append(f"{seconds}s")
    return " ".join(parts) or "0s"


def _duration_match_score(duration_seconds: int, preferred_duration: str) -> int:
    normalized = (preferred_duration or "").strip().lower()
    if not normalized:
        return 1

    target_map = {
        "10 min": 10 * 60,
        "40 min": 40 * 60,
        "60 min": 60 * 60,
        "2 hours": 2 * 60 * 60,
    }
    target = target_map.get(normalized)
    if not target:
        return 1

    diff = abs(duration_seconds - target)
    if diff <= 5 * 60:
        return 4
    if diff <= 15 * 60:
        return 3
    if diff <= 30 * 60:
        return 2
    return 1


def _relevance_score(title: str, channel: str, query: str) -> int:
    title_l = (title or "").lower()
    channel_l = (channel or "").lower()
    tokens = [token for token in re.split(r"[^a-z0-9+]+", (query or "").lower()) if token and token not in {"tutorial", "beginner", "intermediate", "advanced"}]
    score = 0
    for token in tokens:
        if token in title_l:
            score += 3
        elif token in channel_l:
            score += 1
    return score


# MCP Tool Definitions
TOOL_DEFINITIONS = [
    {
        "name": "recommend_topics",
        "description": "Recommend 2-3 high-value learning topics for a selected skill and level. Can exclude already-known topics.",
        "parameters": {
            "type": "object",
            "properties": {
                "skill": {"type": "string", "description": "Skill to learn"},
                "level": {"type": "string", "enum": ["Beginner", "Intermediate", "Advanced"], "description": "User skill level"},
                "count": {"type": "integer", "description": "Number of topics to recommend (max 3)"},
                "exclude_topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Already-known topics that should be replaced"
                },
            },
            "required": ["skill", "level"],
        },
    },
    {
        "name": "search_videos",
        "description": "Search YouTube for learning videos using 6 curated channels and 6 live YouTube results, with optional topic and duration preference.",
        "parameters": {
            "type": "object",
            "properties": {
                "skill": {"type": "string", "description": "The skill to search tutorials for"},
                "level": {"type": "string", "enum": ["Beginner", "Intermediate", "Advanced"], "description": "User skill level"},
                "topic": {"type": "string", "description": "Specific topic inside the skill"},
                "preferred_duration": {"type": "string", "enum": ["10 min", "40 min", "60 min", "2 hours"], "description": "Preferred learning video duration"},
                "max_results": {"type": "integer", "description": "Maximum videos to return (default 12)"},
            },
            "required": ["skill", "level"],
        },
    },
]
