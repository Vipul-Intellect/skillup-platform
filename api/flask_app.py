"""
SkillUp Agent - Flask API
Google GenAI APAC 2026 Hackathon
"""
import os
import sys
import threading
import uuid

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, request, jsonify, render_template, session
from werkzeug.utils import secure_filename

from config.settings import settings
from utils.logger import get_logger

logger = get_logger(__name__)

app = Flask(__name__, 
            template_folder='../templates',
            static_folder='../static')
app.secret_key = settings.FLASK_SECRET_KEY or 'skillup-secret-2026'

# Lazy agent initialization keeps app startup fast and only pays agent cost when needed.
orchestrator = None
learning_agent = None
evaluator_agent = None
_learning_lock = None
_evaluator_lock = None


def get_orchestrator():
    global orchestrator
    if orchestrator is None:
        from agents.orchestrator import OrchestratorAgent
        orchestrator = OrchestratorAgent()
    return orchestrator


def get_learning_agent():
    global learning_agent
    global _learning_lock
    if _learning_lock is None:
        import threading
        _learning_lock = threading.Lock()
    if learning_agent is None:
        with _learning_lock:
            if learning_agent is None:
                from agents.learning_agent import LearningAgent
                learning_agent = LearningAgent()
    return learning_agent


def get_evaluator_agent():
    global evaluator_agent
    global _evaluator_lock
    if _evaluator_lock is None:
        import threading
        _evaluator_lock = threading.Lock()
    if evaluator_agent is None:
        with _evaluator_lock:
            if evaluator_agent is None:
                from agents.evaluator_agent import EvaluatorAgent
                evaluator_agent = EvaluatorAgent()
    return evaluator_agent


def _initialize_agent1():
    """Eagerly initialize Agent 1 so the first UI action is not cold."""
    try:
        get_orchestrator().warm_mcp()
        logger.info("Agent 1 startup warmup completed")
    except Exception as e:
        logger.warning(f"Agent 1 startup warmup failed: {e}")


def _prewarm_market_profiles():
    """Warm hot role market profiles in the background so skill-gap requests stay fast."""
    try:
        from tools.mcp_tools.resume import prewarm_market_profiles

        queued_roles = prewarm_market_profiles()
        logger.info(f"Queued background market profile prewarm for roles: {queued_roles}")
    except Exception as e:
        logger.warning(f"Background market profile prewarm failed: {e}")


if settings.ENABLE_AGENT1_STARTUP_WARMUP:
    _initialize_agent1()
if settings.ENABLE_MARKET_PROFILE_PREWARM:
    threading.Thread(target=_prewarm_market_profiles, daemon=True).start()

# ═══════════════════════════════════════════════════════════════
# HEALTH & SESSION
# ═══════════════════════════════════════════════════════════════

@app.route('/')
def index():
    """Serve main UI"""
    return render_template('index.html')

@app.route('/health')
def health():
    """Health check for Cloud Run"""
    return jsonify({"status": "healthy", "service": "skillup-agent"})

@app.route('/api/session/start', methods=['POST'])
def start_session():
    """Start new learning session"""
    session_id = str(uuid.uuid4())
    session['session_id'] = session_id

    return jsonify({"session_id": session_id, "status": "created"})

# ═══════════════════════════════════════════════════════════════
# ORCHESTRATOR ENDPOINTS (Sync - orchestrator uses asyncio.run internally)
# ═══════════════════════════════════════════════════════════════

@app.route('/api/trending-skills', methods=['GET', 'POST'])
def get_trending_skills():
    """Fetch 7 trending skills via MCP"""
    try:
        target_role = None
        if request.method == 'POST' and request.json:
            target_role = request.json.get('target_role')
        
        result = get_orchestrator().fetch_trending_skills(target_role)
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Trending skills error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/analyze-resume', methods=['POST'])
def analyze_resume():
    """Analyze uploaded resume"""
    try:
        if request.content_type and request.content_type.startswith("multipart/form-data"):
            uploaded_file = request.files.get("resume")
            session_id = request.form.get("session_id") or session.get("session_id")

            if not uploaded_file or not uploaded_file.filename:
                return jsonify({"success": False, "error": "No resume file uploaded"}), 400

            file_name = secure_filename(uploaded_file.filename)
            mime_type = uploaded_file.mimetype or "application/octet-stream"
            file_bytes = uploaded_file.read()

            if not file_bytes:
                return jsonify({"success": False, "error": "Uploaded file is empty"}), 400

            supported_types = {
                "application/pdf",
                "image/png",
                "image/jpeg",
                "image/jpg",
                "image/webp",
            }
            if mime_type not in supported_types:
                return jsonify(
                    {
                        "success": False,
                        "error": f"Unsupported resume file type: {mime_type}",
                    }
                ), 400

            result = get_orchestrator().analyze_resume_document(
                file_name=file_name,
                mime_type=mime_type,
                file_bytes=file_bytes,
                session_id=session_id,
            )
        else:
            data = request.json or {}
            resume_text = data.get('resume_text', '')
            session_id = data.get('session_id') or session.get('session_id')
            
            if not resume_text:
                return jsonify({"success": False, "error": "No resume text"}), 400
            
            result = get_orchestrator().analyze_resume(resume_text, session_id)

        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Resume analysis error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/skill-gaps', methods=['POST'])
def get_skill_gaps():
    """Compute skill gaps and recommendations"""
    try:
        data = request.json or {}
        session_id = data.get('session_id') or session.get('session_id')
        target_role = data.get('target_role', 'Software Developer')
        user_skills = data.get('user_skills')

        result = get_orchestrator().compute_skill_gaps(session_id, target_role, user_skills=user_skills)
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Skill gaps error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/assess-level', methods=['POST'])
def assess_level():
    """Generate level assessment questions"""
    try:
        data = request.json or {}
        skill = data.get('skill')
        declared_level = data.get('declared_level', 'Beginner')
        session_id = data.get('session_id') or session.get('session_id')
        
        result = get_orchestrator().assess_user_level(session_id, skill, declared_level)
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Assessment error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/validate-level', methods=['POST'])
def validate_level():
    """Validate user's level based on answers"""
    try:
        data = request.json or {}
        answers = data.get('answers', [])
        session_id = data.get('session_id') or session.get('session_id')
        questions = data.get('questions')
        declared_level = data.get('declared_level')

        result = get_orchestrator().validate_level(
            session_id,
            answers,
            questions=questions,
            declared_level=declared_level,
        )
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Validation error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# ═══════════════════════════════════════════════════════════════
# LEARNING ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.route('/api/videos', methods=['POST'])
def get_videos():
    """Get video recommendations"""
    try:
        data = request.json or {}
        skill = data.get('skill')
        level = data.get('level', 'Beginner')
        topic = data.get('topic', '')
        preferred_duration = data.get('preferred_duration', '')
        max_results = data.get('max_results', 12)
        
        result = get_learning_agent().search_videos(
            skill,
            level,
            max_results,
            topic,
            preferred_duration,
        )
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Video search error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/topics', methods=['POST'])
def get_topics():
    """Get 2-3 recommended topics for a selected skill and level."""
    try:
        data = request.json or {}
        skill = data.get('skill')
        level = data.get('level', 'Beginner')
        count = data.get('count', 3)
        exclude_topics = data.get('exclude_topics', [])

        result = get_learning_agent().recommend_topics(skill, level, count, exclude_topics)
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Topic recommendation error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/skill-gaps/start', methods=['POST'])
def start_skill_gap_job():
    """Start a background live skill-gap computation job."""
    try:
        data = request.json or {}
        session_id = data.get('session_id') or session.get('session_id')
        target_role = data.get('target_role', 'Software Developer')
        user_skills = data.get('user_skills')

        result = get_orchestrator().start_skill_gap_job(
            session_id=session_id,
            target_role=target_role,
            user_skills=user_skills,
        )
        return jsonify({"success": True, "data": result}), 202
    except Exception as e:
        logger.error(f"Skill-gap job start error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/skill-gaps/<job_id>', methods=['GET'])
def get_skill_gap_job(job_id):
    """Get background skill-gap job status or final result."""
    try:
        result = get_orchestrator().get_skill_gap_job_status(job_id)
        if result.get("error") == "Job not found":
            return jsonify({"success": False, "error": "Job not found"}), 404
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Skill-gap job status error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/execute-code', methods=['POST'])
def execute_code():
    """Execute code via Piston API"""
    try:
        data = request.json or {}
        code = data.get('code', '')
        language = data.get('language', 'python')
        stdin = data.get('stdin', '')
        context = data.get('context', {})
        
        result = get_learning_agent().execute_code(code, language, stdin, context)
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Code execution error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/validate-code', methods=['POST'])
def validate_code():
    """Validate code syntax without executing."""
    try:
        data = request.json or {}
        code = data.get('code', '')
        language = data.get('language', 'python')

        result = get_learning_agent().validate_code(code, language)
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Code validation error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/execution-config', methods=['GET'])
def get_execution_config():
    """Return runtime/editor config for the future Monaco workspace."""
    try:
        result = get_learning_agent().get_execution_config()
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Execution config error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/practice', methods=['POST'])
def generate_practice():
    """Generate topic practice pack with questions and one mini-lab."""
    try:
        data = request.json or {}
        session_id = data.get('session_id') or session.get('session_id')
        skill = data.get('skill')
        topic = data.get('topic')
        level = data.get('level', 'Beginner')
        language = data.get('language', 'python')
        video_context = data.get('video_context', {})

        result = get_learning_agent().generate_practice(
            session_id=session_id,
            skill=skill,
            topic=topic,
            level=level,
            language=language,
            video_context=video_context,
        )
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Practice generation error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/practice/evaluate', methods=['POST'])
def evaluate_practice():
    """Evaluate learner-written answers for the active practice pack."""
    try:
        data = request.json or {}
        session_id = data.get('session_id') or session.get('session_id')
        skill = data.get('skill')
        topic = data.get('topic')
        answers = data.get('answers', [])

        result = get_learning_agent().evaluate_practice_answers(
            session_id=session_id,
            skill=skill,
            topic=topic,
            answers=answers,
        )
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Practice evaluation error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/hint', methods=['POST'])
def get_hint():
    """Get Socratic hint"""
    try:
        data = request.json or {}
        session_id = data.get('session_id') or session.get('session_id')
        skill = data.get('skill')
        topic = data.get('topic')
        level = data.get('level', 'Beginner')
        code = data.get('code', '')
        error = data.get('error', '')
        hint_level = data.get('hint_level', 1)
        
        result = get_learning_agent().get_hint(
            session_id, skill, topic, level, code, error, hint_level
        )
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Hint error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/schedule', methods=['POST'])
def generate_schedule():
    """Generate learning schedule"""
    try:
        data = request.json or {}
        session_id = data.get('session_id') or session.get('session_id')
        skill = data.get('skill')
        level = data.get('level', 'Beginner')
        mode = data.get('mode', 'balanced')
        daily_time = data.get('daily_time', 60)
        
        result = get_learning_agent().generate_schedule(
            session_id, skill, level, mode, daily_time
        )
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Schedule error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# ═══════════════════════════════════════════════════════════════
# EVALUATOR ENDPOINTS (TO BE CONNECTED)
# ═══════════════════════════════════════════════════════════════

@app.route('/api/schedule/<session_id>', methods=['GET'])
def get_schedule(session_id):
    """Get current learning schedule for a session."""
    try:
        result = get_learning_agent().get_schedule(session_id)
        if result.get("error") == "Session not found":
            return jsonify({"success": False, "error": "Session not found"}), 404
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Get schedule error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/schedule/progress', methods=['POST'])
def update_schedule_progress():
    """Update learning schedule progress for a session."""
    try:
        data = request.json or {}
        session_id = data.get('session_id') or session.get('session_id')
        day = int(data.get('day', 1))
        completed = data.get('completed', True)

        result = get_learning_agent().update_progress(
            session_id=session_id,
            day=day,
            completed=completed,
        )
        if result.get("error") == "Session not found":
            return jsonify({"success": False, "error": "Session not found"}), 404
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Update schedule progress error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/evaluate', methods=['POST'])
def evaluate():
    """Generate an evaluation pack or score submitted answers."""
    try:
        data = request.json or {}
        session_id = data.get('session_id') or session.get('session_id')
        skill = data.get('skill')
        level = data.get('level')
        answers = data.get('answers', [])

        if answers:
            result = get_evaluator_agent().evaluate_answers(
                session_id=session_id,
                skill=skill,
                level=level,
                answers=answers,
                questions=data.get('questions'),
                practice_summary=data.get('practice_summary'),
            )
        else:
            result = get_evaluator_agent().generate_evaluation(
                session_id=session_id,
                skill=skill,
                level=level or 'Beginner',
                question_count=int(data.get('question_count', 5)),
            )

        if result.get("error"):
            return jsonify({"success": False, "error": result["error"]}), 400
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Evaluation error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/jobs', methods=['POST'])
def get_jobs():
    """Fetch relevant jobs for a skill and current readiness."""
    try:
        data = request.json or {}
        session_id = data.get('session_id') or session.get('session_id')
        skill = data.get('skill')
        level = data.get('level', '')
        limit = int(data.get('limit', 10))

        result = get_evaluator_agent().fetch_jobs(skill, level, limit, session_id=session_id or "")
        if result.get("error"):
            return jsonify({"success": False, "error": result["error"], "data": result}), 400
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Jobs error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/results/<session_id>', methods=['GET'])
def get_results(session_id):
    """Get session results"""
    try:
        result = get_evaluator_agent().get_result(session_id)
        if result and not result.get("error"):
            return jsonify({"success": True, "data": result})
        return jsonify({"success": False, "error": "No results found"}), 404
    except Exception as e:
        logger.error(f"Results error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# ═══════════════════════════════════════════════════════════════
# ERROR HANDLERS
# ═══════════════════════════════════════════════════════════════

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Internal server error"}), 500

# ═══════════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
