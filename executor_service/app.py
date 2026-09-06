import os
import logging

from flask import Flask, jsonify, request

from runtime import execute_code, get_execution_config, validate_code

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)


def _is_authorized(req) -> bool:
    shared_secret = (os.getenv("EXECUTOR_SHARED_SECRET") or "").strip()
    if not shared_secret:
        return True
    provided = req.headers.get("X-Executor-Secret", "")
    return provided == shared_secret


@app.before_request
def authorize():
    if request.path == "/health":
        return None

    if not _is_authorized(request):
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    return None


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "service": "skillup-executor"})


@app.route("/config", methods=["GET"])
def config():
    return jsonify({"success": True, "data": get_execution_config()})


@app.route("/validate", methods=["POST"])
def validate():
    try:
        payload = request.get_json(silent=True) or {}
        result = validate_code(
            code=payload.get("code", ""),
            language=payload.get("language", "python"),
        )
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Executor validate error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/execute", methods=["POST"])
def execute():
    try:
        payload = request.get_json(silent=True) or {}
        result = execute_code(
            code=payload.get("code", ""),
            language=payload.get("language", "python"),
            stdin=payload.get("stdin", ""),
            timeout_seconds=payload.get("timeout_seconds", 5),
        )
        return jsonify({"success": True, "data": result})
    except Exception as e:
        logger.error(f"Executor run error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=False)
