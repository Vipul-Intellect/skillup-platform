import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any

import requests
from google import genai
from google.genai import types

from config.settings import settings
from database.firestore_client import (
    get_cache,
    get_result,
    get_session,
    set_cache,
)
from utils.logger import get_logger

logger = get_logger(__name__)

MODEL_ID = "gemini-3.6-flash"
_client = None
_genai_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="evaluator-genai")

QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "minItems": 5,
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "type": {
                        "type": "string",
                        "enum": ["multiple_choice", "short_answer"],
                    },
                    "question": {"type": "string"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "correct_answer": {"type": "string"},
                    "expected_keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "difficulty": {
                        "type": "string",
                        "enum": ["easy", "medium", "hard"],
                    },
                    "focus_area": {"type": "string"},
                },
                "required": ["id", "type", "question", "difficulty", "focus_area"],
                "additionalProperties": False,
            },
        },
        "instructions": {"type": "string"},
    },
    "required": ["questions", "instructions"],
    "additionalProperties": False,
}

CORE_EVALUATION_SCHEMA = {
    "type": "object",
    "properties": {
        "item_scores": {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 1,
            "maxItems": 5,
        },
        "total_score": {"type": "integer"},
        "badge": {
            "type": "string",
            "enum": ["Needs Practice", "Beginner", "Intermediate", "Advanced", "Expert"],
        },
        "readiness": {
            "type": "string",
            "enum": [
                "Revise Fundamentals",
                "Practice More",
                "Project Ready",
                "Interview Ready",
                "Job Ready",
            ],
        },
        "confidence": {"type": "number"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "weak_topics": {"type": "array", "items": {"type": "string"}},
        "feedback": {"type": "string"},
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "item_scores",
        "total_score",
        "badge",
        "readiness",
        "confidence",
        "strengths",
        "weak_topics",
        "feedback",
        "next_steps",
    ],
    "additionalProperties": False,
}


def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.GEMINI_API_KEY)
    return _client


def _generate_json_now(prompt: str, schema: dict, *, max_output_tokens: int) -> dict[str, Any]:
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
    text = response.text
    if not text:
        try:
            finish_reason = response.candidates[0].finish_reason
        except (IndexError, AttributeError):
            finish_reason = "UNKNOWN"
        raise ValueError(f"Model returned empty response. Finish reason: {finish_reason}")
    return json.loads(text)


def _generate_json(prompt: str, schema: dict, *, max_output_tokens: int) -> dict[str, Any]:
    future = _genai_executor.submit(
        _generate_json_now,
        prompt,
        schema,
        max_output_tokens=max_output_tokens,
    )
    try:
        return future.result(timeout=max(settings.API_TIMEOUT + 10, 20))
    except FuturesTimeoutError as e:
        future.cancel()
        raise TimeoutError(f"Gemini request timed out after {settings.API_TIMEOUT}s") from e


def _safe_ratio(numerator: int, denominator: int) -> float:
    if not denominator:
        return 0.0
    return round(float(numerator) / float(denominator), 2)


def _score_to_demonstrated_level(total_score: int) -> str:
    if total_score >= 85:
        return "Advanced"
    if total_score >= 60:
        return "Intermediate"
    return "Beginner"


def _normalize_practice_summary(session_data: dict, practice_summary: dict | None) -> dict:
    session_practice = dict(session_data.get("last_practice_evaluation") or {})
    provided_summary = dict(practice_summary or {})
    merged = {**session_practice, **provided_summary}

    accepted_count = int(merged.get("accepted_count", 0) or 0)
    total_questions = int(merged.get("total_questions", 0) or 0)
    merged["accepted_count"] = accepted_count
    merged["total_questions"] = total_questions
    merged["acceptance_ratio"] = _safe_ratio(accepted_count, total_questions)
    return merged


def _normalize_hint_summary(session_data: dict) -> dict:
    hint_usage = session_data.get("hint_usage") or {}
    total_hint_requests = 0
    max_hint_level = 0
    weak_topics = []

    for key, data in hint_usage.items():
        count = int(data.get("count", 0) or 0)
        max_level = int(data.get("max_level", 0) or 0)
        total_hint_requests += count
        max_hint_level = max(max_hint_level, max_level)

        if ":" in key:
            _, topic = key.split(":", 1)
        else:
            topic = key

        if count >= 3 or max_level >= 3:
            weak_topics.append(topic)

    if max_hint_level >= 3 or total_hint_requests >= 6:
        hint_dependency = "High"
    elif max_hint_level >= 2 or total_hint_requests >= 3:
        hint_dependency = "Moderate"
    else:
        hint_dependency = "Low"

    return {
        "total_hint_requests": total_hint_requests,
        "max_hint_level": max_hint_level,
        "weak_topics": weak_topics[:5],
        "hint_dependency": hint_dependency,
    }


def _build_mastery_summary(
    validated_level: str,
    demonstrated_level: str,
    practice_summary: dict,
    readiness: str,
) -> dict:
    acceptance_ratio = float(practice_summary.get("acceptance_ratio", 0.0) or 0.0)
    if acceptance_ratio >= 0.75:
        concept_grasp = "Strong"
    elif acceptance_ratio >= 0.4:
        concept_grasp = "Developing"
    else:
        concept_grasp = "Emerging"

    return {
        "validated_level": validated_level or demonstrated_level,
        "demonstrated_level": demonstrated_level,
        "concept_grasp": concept_grasp,
        "practical_readiness": readiness,
    }


def _build_independence_signal(practice_summary: dict, hint_summary: dict) -> dict:
    acceptance_ratio = float(practice_summary.get("acceptance_ratio", 0.0) or 0.0)
    hint_dependency = hint_summary.get("hint_dependency", "Low")

    if acceptance_ratio >= 0.75:
        practice_consistency = "High"
    elif acceptance_ratio >= 0.4:
        practice_consistency = "Moderate"
    else:
        practice_consistency = "Low"

    if hint_dependency == "Low" and practice_consistency == "High":
        summary = "Learner is solving with solid independence and consistent practice quality."
    elif hint_dependency == "High":
        summary = "Learner is progressing, but still depends heavily on hints for difficult steps."
    else:
        summary = "Learner shows partial independence and should reinforce weak topics with more practice."

    return {
        "hint_dependency": hint_dependency,
        "practice_consistency": practice_consistency,
        "summary": summary,
    }


def _build_achievements(
    total_score: int,
    readiness: str,
    confidence: float,
    independence_signal: dict,
) -> list[str]:
    achievements = ["Assessment Finisher"]

    if total_score >= 75:
        achievements.append("Strong Concept Grip")
    if readiness in {"Interview Ready", "Job Ready"}:
        achievements.append("Readiness Milestone")
    if confidence >= 0.8 and independence_signal.get("hint_dependency") == "Low":
        achievements.append("Independent Problem Solver")
    if independence_signal.get("practice_consistency") == "High":
        achievements.append("Consistent Learner")

    return achievements[:4]


def _build_job_fit(skill: str, readiness: str, weak_topics: list[str]) -> dict:
    skill = (skill or "Software").strip()

    if readiness == "Job Ready":
        current_fit = [f"Junior {skill} Developer", f"{skill} Engineer"]
        stretch_fit = [f"Mid-level {skill} Developer", f"{skill} Specialist"]
    elif readiness == "Interview Ready":
        current_fit = [f"{skill} Intern", f"Junior {skill} Developer"]
        stretch_fit = [f"Associate {skill} Engineer", f"Project-based {skill} Role"]
    elif readiness == "Project Ready":
        current_fit = [f"{skill} Intern", f"Trainee {skill} Developer"]
        stretch_fit = [f"Junior {skill} Developer"]
    else:
        current_fit = [f"Learning-focused {skill} projects", f"{skill} internship preparation"]
        stretch_fit = [f"{skill} Intern"]

    missing_for_next_level = weak_topics[:3] or [f"Deeper {skill} fundamentals", "Independent practice", "Applied projects"]

    return {
        "current_fit": current_fit,
        "stretch_fit": stretch_fit,
        "missing_for_next_level": missing_for_next_level,
    }


def _build_final_report(
    skill: str,
    total_score: int,
    readiness: str,
    strengths: list[str],
    weak_topics: list[str],
) -> dict:
    primary_strength = strengths[0] if strengths else f"{skill} fundamentals"
    primary_gap = weak_topics[0] if weak_topics else "consistency under independent practice"

    return {
        "headline": f"{readiness} in {skill}",
        "summary": (
            f"The learner scored {total_score}/100 in {skill} and currently shows strongest performance in "
            f"{primary_strength}. The main improvement area is {primary_gap}."
        ),
        "recommended_focus": f"Reinforce {primary_gap} with one more focused practice cycle before moving up in difficulty.",
    }


def _fallback_questions(skill: str, level: str, question_count: int) -> list[dict]:
    normalized_skill = (skill or "the skill").strip()
    return [
        {
            "id": 1,
            "type": "multiple_choice",
            "question": f"What is the main purpose of using {normalized_skill} in a real project?",
            "options": [
                "To structure and solve practical problems",
                "Only to memorize syntax",
                "To avoid debugging completely",
                "To replace system design",
            ],
            "correct_answer": "To structure and solve practical problems",
            "expected_keywords": [],
            "difficulty": "easy",
            "focus_area": "fundamentals",
        },
        {
            "id": 2,
            "type": "multiple_choice",
            "question": f"When you get stuck in {normalized_skill}, what is the best next move?",
            "options": [
                "Break the task into smaller steps",
                "Rewrite everything immediately",
                "Ignore the error message",
                "Skip testing",
            ],
            "correct_answer": "Break the task into smaller steps",
            "expected_keywords": [],
            "difficulty": "easy",
            "focus_area": "problem_solving",
        },
        {
            "id": 3,
            "type": "multiple_choice",
            "question": f"What usually improves maintainability in {normalized_skill} work?",
            "options": [
                "Reusable, readable logic",
                "Longer files with repeated code",
                "Avoiding comments and naming",
                "Changing many things at once",
            ],
            "correct_answer": "Reusable, readable logic",
            "expected_keywords": [],
            "difficulty": "medium",
            "focus_area": "code_quality",
        },
        {
            "id": 4,
            "type": "short_answer",
            "question": f"Explain one situation where {normalized_skill} would help you solve a real task.",
            "options": [],
            "correct_answer": "",
            "expected_keywords": ["problem", "solution", "task"],
            "difficulty": "medium",
            "focus_area": "applied_understanding",
        },
        {
            "id": 5,
            "type": "short_answer",
            "question": f"What would you check first if your {normalized_skill} solution is not working as expected?",
            "options": [],
            "correct_answer": "",
            "expected_keywords": ["input", "logic", "error", "test", "debug"],
            "difficulty": "hard" if (level or "").lower() == "advanced" else "medium",
            "focus_area": "debugging",
        },
    ][:question_count]


def _fallback_evaluation_result(
    resolved_questions: list,
    answers: list,
    skill: str,
    level: str,
    practice_context: dict,
    hint_summary: dict,
) -> dict:
    item_scores = []
    strengths = []
    weak_topics = []

    for question, answer in zip(resolved_questions, answers):
        answer_text = str(answer or "").strip().lower()
        focus_area = question.get("focus_area", "general")
        q_type = question.get("type", "")

        if q_type == "multiple_choice":
            expected = str(question.get("correct_answer", "")).strip().lower()
            correct = answer_text == expected
            score = 20 if correct else 5
            if correct:
                strengths.append(focus_area)
            else:
                weak_topics.append(focus_area)
        else:
            keywords = [str(keyword).strip().lower() for keyword in question.get("expected_keywords", [])]
            matches = sum(1 for keyword in keywords if keyword and keyword in answer_text)
            if matches >= 2:
                score = 18
                strengths.append(focus_area)
            elif matches == 1:
                score = 12
                weak_topics.append(focus_area)
            else:
                score = 6
                weak_topics.append(focus_area)

        item_scores.append(score)

    if len(item_scores) < len(resolved_questions):
        missing = len(resolved_questions) - len(item_scores)
        item_scores.extend([0] * missing)
        weak_topics.extend(
            question.get("focus_area", "general")
            for question in resolved_questions[len(answers):]
        )

    total_score = max(0, min(100, sum(item_scores)))
    if total_score >= 90:
        badge = "Expert"
        readiness = "Job Ready"
        confidence = 0.88
    elif total_score >= 75:
        badge = "Advanced"
        readiness = "Interview Ready"
        confidence = 0.78
    elif total_score >= 60:
        badge = "Intermediate"
        readiness = "Project Ready"
        confidence = 0.68
    elif total_score >= 40:
        badge = "Beginner"
        readiness = "Practice More"
        confidence = 0.56
    else:
        badge = "Needs Practice"
        readiness = "Revise Fundamentals"
        confidence = 0.42

    if hint_summary.get("hint_dependency") == "High":
        confidence = max(0.3, confidence - 0.12)
    if float(practice_context.get("acceptance_ratio", 0.0) or 0.0) >= 0.75:
        confidence = min(0.95, confidence + 0.05)

    strengths = list(dict.fromkeys(strengths or [f"{skill} fundamentals"]))
    weak_topics = list(dict.fromkeys(weak_topics))

    return {
        "item_scores": item_scores[:5],
        "total_score": total_score,
        "badge": badge,
        "readiness": readiness,
        "confidence": round(confidence, 2),
        "strengths": strengths[:4],
        "weak_topics": weak_topics[:5],
        "feedback": f"Fallback evaluation used because Gemini timed out. The learner currently shows {badge.lower()} performance in {skill}.",
        "next_steps": [
            f"Review the weakest {skill} topic from this evaluation.",
            "Complete one more independent practice cycle without hints.",
            f"Retake the {level} evaluation after revision.",
        ],
    }


def generate_evaluation(session_id: str, skill: str, level: str, question_count: int = 5) -> dict:
    try:
        if not session_id:
            return {"error": "Session ID is required"}
        if not skill:
            return {"error": "Skill is required"}
        prompt = f"""Create exactly {question_count} evaluation questions for {skill} at {level} level.

Use this mix:
- 3 multiple choice questions with 4 options
- 2 short answer questions

Focus on real understanding, not trivia.
Return concise questions and practical focus areas.
For MCQ include the correct answer.
For short answers include 2-4 expected keywords.
"""

        try:
            result = _generate_json(prompt, QUESTION_SCHEMA, max_output_tokens=4096)
            instructions = result.get(
                "instructions",
                "Answer honestly. Short-answer responses can be brief but should be specific.",
            )
        except TimeoutError:
            logger.warning(f"Gemini timed out generating evaluation for {session_id}; using fallback questions")
            result = {"questions": _fallback_questions(skill, level, question_count)}
            instructions = "Fallback evaluation pack generated because the model timed out. Answers can still be evaluated normally."

        payload = {
            "session_id": session_id,
            "skill": skill,
            "level": level,
            "question_count": question_count,
            "questions": result.get("questions", []),
            "instructions": instructions,
        }
        return payload
    except Exception as e:
        logger.error(f"Error generating evaluation for {session_id}: {e}")
        return {"error": str(e)}


def evaluate_answers(
    session_id: str,
    skill: str,
    level: str,
    answers: list,
    questions: list | None = None,
    practice_summary: dict | None = None,
    evaluation_context: dict | None = None,
) -> dict:
    try:
        if not session_id:
            return {"error": "Session ID is required"}

        session_data = dict(evaluation_context or {})
        if not session_data:
            session_data = get_session(session_id) or {}
        resolved_questions = questions or []
        if not resolved_questions:
            if not session_data:
                return {"error": "Session not found"}
            resolved_questions = session_data.get("evaluation_questions", [])
            if not skill:
                skill = session_data.get("evaluation_skill", skill)
            if not level:
                level = session_data.get("evaluation_level", level)

        if not resolved_questions:
            return {"error": "No evaluation questions available"}

        validated_level = session_data.get("validated_level", level or "Beginner")
        level_confidence = float(session_data.get("level_confidence", 0.5) or 0.5)
        practice_context = _normalize_practice_summary(session_data, practice_summary)
        hint_summary = _normalize_hint_summary(session_data)

        prompt = f"""Evaluate this learner's {skill} assessment at {level} level.

Questions:
{json.dumps(resolved_questions, indent=2)}

Answers:
{json.dumps(answers, indent=2)}

Practice context:
{json.dumps(practice_context, indent=2)}

Validated level context:
{json.dumps({"validated_level": validated_level, "level_confidence": level_confidence}, indent=2)}

Hint dependence context:
{json.dumps(hint_summary, indent=2)}

Score each answer out of 20.
Then produce:
- total score out of 100
- badge
- readiness level
- confidence 0.0 to 1.0
- strengths
- weak topics
- brief actionable feedback
- 3 next steps

Guidance:
- Use the practice and hint context to adjust confidence and weak topics honestly.
- If hint dependence is high, confidence should not be overstated.
- If practice acceptance is strong, reflect that in strengths and readiness.

Badge mapping guidance:
- 90-100: Expert
- 75-89: Advanced
- 60-74: Intermediate
- 40-59: Beginner
- below 40: Needs Practice

Readiness guidance:
- low score: Revise Fundamentals / Practice More
- mid score: Project Ready
- strong score: Interview Ready
- top score: Job Ready
"""

        try:
            result = _generate_json(prompt, CORE_EVALUATION_SCHEMA, max_output_tokens=4096)
        except TimeoutError:
            logger.warning(f"Gemini timed out evaluating answers for {session_id}; using deterministic scoring")
            result = _fallback_evaluation_result(
                resolved_questions=resolved_questions,
                answers=answers,
                skill=skill,
                level=level,
                practice_context=practice_context,
                hint_summary=hint_summary,
            )
        total_score = int(result.get("total_score", 0) or 0)
        demonstrated_level = _score_to_demonstrated_level(total_score)

        merged_weak_topics = list(dict.fromkeys(
            (result.get("weak_topics") or []) +
            (hint_summary.get("weak_topics") or [])
        ))

        independence_signal = _build_independence_signal(practice_context, hint_summary)
        final_payload = {
            "session_id": session_id,
            "skill": skill,
            "level": level,
            "answers_submitted": len(answers or []),
            **result,
            "total_score": total_score,
            "weak_topics": merged_weak_topics,
            "mastery_summary": _build_mastery_summary(
                validated_level=validated_level,
                demonstrated_level=demonstrated_level,
                practice_summary=practice_context,
                readiness=result.get("readiness", "Practice More"),
            ),
            "independence_signal": independence_signal,
            "achievements": _build_achievements(
                total_score=total_score,
                readiness=result.get("readiness", "Practice More"),
                confidence=float(result.get("confidence", 0.5) or 0.5),
                independence_signal=independence_signal,
            ),
            "job_fit": _build_job_fit(
                skill=skill,
                readiness=result.get("readiness", "Practice More"),
                weak_topics=merged_weak_topics,
            ),
            "final_report": _build_final_report(
                skill=skill,
                total_score=total_score,
                readiness=result.get("readiness", "Practice More"),
                strengths=result.get("strengths", []),
                weak_topics=merged_weak_topics,
            ),
            "learning_signals": {
                "validated_level": validated_level,
                "level_confidence": level_confidence,
                "practice_acceptance_ratio": practice_context.get("acceptance_ratio", 0.0),
                "hint_dependency": hint_summary.get("hint_dependency", "Low"),
                "total_hint_requests": hint_summary.get("total_hint_requests", 0),
            },
        }

        return final_payload
    except Exception as e:
        logger.error(f"Error evaluating answers for {session_id}: {e}")
        return {"error": str(e)}


def fetch_jobs(
    skill: str,
    level: str = "",
    limit: int = 10,
    session_id: str = "",
    readiness_override: str = "",
) -> dict:
    if not skill:
        return {"error": "Skill is required", "jobs": [], "count": 0}

    resolved_level = (level or "").strip()
    readiness = (readiness_override or "").strip()
    if session_id and not resolved_level and not readiness:
        prior_result = get_result(session_id) or {}
        readiness = prior_result.get("readiness", "")
        if readiness == "Job Ready":
            resolved_level = "Advanced"
        elif readiness == "Interview Ready":
            resolved_level = "Intermediate"
        elif readiness == "Project Ready":
            resolved_level = "Beginner"

    cache_key = f"jobs:v2:{skill.strip().lower()}:{resolved_level.strip().lower()}:{limit}"
    cached = get_cache(cache_key)
    if cached:
        if "job_fit" not in cached:
            cached["job_fit"] = _build_job_fit(
                skill,
                readiness or ("Project Ready" if resolved_level else "Practice More"),
                [],
            )
        return cached

    role = "developer"
    if session_id:
        session_data = get_session(session_id) or {}
        role = session_data.get("target_role") or session_data.get("domain") or "developer"

    try:
        query_parts = [skill.strip(), role.strip()]
        if resolved_level:
            level_key = resolved_level.strip().lower()
            if level_key == "beginner":
                query_parts.insert(0, "junior")
            elif level_key == "intermediate":
                query_parts.insert(0, "mid level")
            elif level_key == "advanced":
                query_parts.insert(0, "senior")
                
        job_data = []
        for time_filter in ["week", "month"]:
            response = requests.get(
                "https://jsearch.p.rapidapi.com/search",
                headers={
                    "X-RapidAPI-Key": settings.RAPIDAPI_KEY,
                    "X-RapidAPI-Host": settings.RAPIDAPI_JSEARCH_HOST,
                },
                params={
                    "query": " ".join([part for part in query_parts if part]),
                    "country": "in",
                    "num_pages": "1",
                    "date_posted": time_filter,
                },
                timeout=settings.API_TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()
            job_data = data.get("data") or []
            if job_data:
                break

        jobs = []
        for job in job_data[:limit]:
            jobs.append(
                {
                    "title": job.get("job_title"),
                    "company": job.get("employer_name"),
                    "location": ", ".join(
                        part
                        for part in [
                            job.get("job_city"),
                            job.get("job_state"),
                            job.get("job_country"),
                        ]
                        if part
                    )
                    or "Remote / Not specified",
                    "employment_type": job.get("job_employment_type") or "Not specified",
                    "apply_url": job.get("job_apply_link"),
                    "posted_at": job.get("job_posted_at_datetime_utc"),
                    "description": (job.get("job_description") or "")[:240],
                }
            )

        result = {
            "skill": skill,
            "level": resolved_level,
            "count": len(jobs),
            "jobs": jobs,
            "job_fit": _build_job_fit(
                skill,
                readiness or ("Project Ready" if resolved_level else "Practice More"),
                [],
            ),
        }
        set_cache(cache_key, result, ttl_hours=settings.JOB_CACHE_TTL)
        return result
    except Exception as e:
        logger.error(f"Error fetching jobs for {skill}: {e}")
        return {"skill": skill, "level": resolved_level, "count": 0, "jobs": [], "error": str(e)}


def get_evaluation_result(session_id: str) -> dict:
    try:
        result = get_result(session_id)
        if result:
            return result
        return {"error": "No results found"}
    except Exception as e:
        logger.error(f"Error getting evaluation result for {session_id}: {e}")
        return {"error": str(e)}
