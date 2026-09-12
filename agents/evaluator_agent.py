"""
Evaluator Agent - Handles learning evaluation, readiness scoring, and job discovery.
"""
import asyncio
import json
from json import JSONDecodeError
import os
import sys
import threading

from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from google import genai
from google.genai import types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from a2a.protocol import A2AMessage, A2AProtocol, a2a_protocol
from config.settings import settings
from database.firestore_client import get_result, get_session, save_result, save_session
from utils.logger import get_logger

logger = get_logger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVALUATOR_SERVER_PATH = os.path.join(PROJECT_ROOT, "tools", "mcp_tools", "evaluator_server.py")


def _child_process_env() -> dict:
    env = os.environ.copy()
    existing_path = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = PROJECT_ROOT if not existing_path else f"{PROJECT_ROOT}{os.pathsep}{existing_path}"
    return env


class EvaluatorAgent:
    """
    Evaluator Agent responsibilities:
    - Generate evaluation packs
    - Score learner answers and produce mastery/readiness results
    - Fetch relevant jobs
    - Expose evaluation results for later retrieval
    """

    def __init__(self):
        self.client = genai.Client(api_key=settings.GEMINI_API_KEY)
        self.model_id = "gemini-3.6-flash"

        self.server_params = StdioServerParameters(
            command=sys.executable,
            args=[EVALUATOR_SERVER_PATH],
            env=_child_process_env(),
        )

        self.loop = asyncio.new_event_loop()
        self._loop_lock = threading.Lock()
        self._stdio_context = None
        self._session_context = None
        self.mcp_session = None
        self.tools = []
        self.adk_app_name = "skillup_evaluator"

        self.adk_connection_params = StdioConnectionParams(
            server_params=self.server_params,
            timeout=float(settings.API_TIMEOUT),
        )
        self.adk_toolset = McpToolset(connection_params=self.adk_connection_params)
        self.adk_agent = LlmAgent(
            name="evaluator",
            model=self.model_id,
            description="SkillUp evaluator agent for assessments, readiness scoring, and jobs.",
            instruction="""You are the SkillUp evaluator agent.

Use the available MCP tools to generate evaluation questions, score learner answers,
assess readiness, and fetch job recommendations. Prefer tool-grounded results and
keep outputs structured and practical.""",
            tools=[self.adk_toolset],
            generate_content_config=types.GenerateContentConfig(
            ),
        )
        self.adk_runner = InMemoryRunner(agent=self.adk_agent, app_name=self.adk_app_name)

        a2a_protocol.register_agent("evaluator", self)
        logger.info("EvaluatorAgent initialized with ADK runtime and MCP")

    async def _ensure_mcp_session(self):
        if self.mcp_session is not None:
            return self.mcp_session

        self._stdio_context = stdio_client(self.server_params)
        read, write = await self._stdio_context.__aenter__()
        self._session_context = ClientSession(read, write)
        self.mcp_session = await self._session_context.__aenter__()
        await self.mcp_session.initialize()

        tools_response = await self.mcp_session.list_tools()
        self.tools = tools_response.tools
        return self.mcp_session

    async def _reset_mcp_session(self):
        try:
            if self._session_context is not None:
                await self._session_context.__aexit__(None, None, None)
        except Exception:
            pass

        try:
            if self._stdio_context is not None:
                await self._stdio_context.__aexit__(None, None, None)
        except Exception:
            pass

        self._session_context = None
        self._stdio_context = None
        self.mcp_session = None
        self.tools = []

    def _run_on_loop(self, coro):
        with self._loop_lock:
            return self.loop.run_until_complete(coro)

    def warm_mcp(self):
        try:
            self._run_on_loop(self._ensure_mcp_session())
            logger.info("Evaluator MCP session warmed")
            return True
        except Exception as e:
            logger.warning(f"Evaluator MCP warmup skipped: {e}")
            return False

    async def _call_mcp_tool(self, tool_name: str, arguments: dict):
        try:
            session = await self._ensure_mcp_session()
            result = await session.call_tool(tool_name, arguments)

            if result.content:
                for content in result.content:
                    if hasattr(content, "text"):
                        text = (content.text or "").strip()
                        if not text:
                            continue
                        try:
                            return json.loads(text)
                        except JSONDecodeError:
                            logger.warning(f"Skipping non-JSON MCP text chunk from {tool_name}: {text[:200]}")
                            continue

            return {"error": "No response from tool"}
        except Exception as e:
            logger.error(f"Evaluator MCP tool call error: {tool_name} - {e}")
            await self._reset_mcp_session()
            return {"error": str(e)}

    def get_adk_agent(self):
        return self.adk_agent

    def get_adk_runner(self):
        return self.adk_runner

    def generate_evaluation(
        self,
        session_id: str,
        skill: str,
        level: str,
        question_count: int = 5,
    ) -> dict:
        result = self._run_on_loop(
            self._call_mcp_tool(
                "generate_evaluation",
                {
                    "session_id": session_id,
                    "skill": skill,
                    "level": level,
                    "question_count": question_count,
                },
            )
        )
        if not result.get("error"):
            if not self._persist_generated_evaluation(session_id, skill, level, result):
                return {"error": "Failed to persist evaluation questions"}
        return result

    def evaluate_answers(
        self,
        session_id: str,
        skill: str,
        level: str,
        answers: list,
        questions: list | None = None,
        practice_summary: dict | None = None,
    ) -> dict:
        session_data = get_session(session_id) or {}
        resolved_questions = questions or session_data.get("evaluation_questions", [])
        resolved_skill = skill or session_data.get("evaluation_skill", skill)
        resolved_level = level or session_data.get("evaluation_level", level) or "Beginner"
        evaluation_context = {
            "validated_level": session_data.get("validated_level", resolved_level),
            "level_confidence": session_data.get("level_confidence", 0.5),
            "last_practice_evaluation": session_data.get("last_practice_evaluation", {}),
            "hint_usage": session_data.get("hint_usage", {}),
            "evaluation_questions": resolved_questions,
            "evaluation_skill": resolved_skill,
            "evaluation_level": resolved_level,
        }
        arguments = {
            "session_id": session_id,
            "skill": resolved_skill,
            "level": resolved_level,
            "answers": answers,
            "evaluation_context": evaluation_context,
        }
        if resolved_questions:
            arguments["questions"] = resolved_questions
        if practice_summary:
            arguments["practice_summary"] = practice_summary

        result = self._run_on_loop(
            self._call_mcp_tool(
                "evaluate_answers",
                arguments,
            )
        )
        if not result.get("error"):
            if not self._persist_scored_evaluation(session_id, result):
                return {"error": "Failed to persist evaluation result"}
        return result

    def _persist_generated_evaluation(self, session_id: str, skill: str, level: str, result: dict) -> bool:
        return save_session(
            session_id,
            {
                "evaluation_skill": skill,
                "evaluation_level": level,
                "evaluation_questions": result.get("questions", []),
            },
        )

    def _persist_scored_evaluation(self, session_id: str, result: dict) -> bool:
        if not save_result(session_id, result):
            return False
        save_session(
            session_id,
            {
                "last_evaluation_score": result.get("total_score"),
                "last_evaluation_badge": result.get("badge"),
                "last_evaluation_readiness": result.get("readiness"),
                "last_evaluation_weak_topics": result.get("weak_topics", []),
                "last_evaluation_achievements": result.get("achievements", []),
            },
        )
        return True

    def fetch_jobs(self, skill: str, level: str = "", limit: int = 10, session_id: str = "") -> dict:
        readiness_override = ""
        resolved_level = level
        if session_id and not resolved_level:
            prior_result = get_result(session_id) or {}
            readiness_override = prior_result.get("readiness", "")
            if readiness_override == "Job Ready":
                resolved_level = "Advanced"
            elif readiness_override == "Interview Ready":
                resolved_level = "Intermediate"
            elif readiness_override == "Project Ready":
                resolved_level = "Beginner"
        from tools.mcp_tools.evaluator import fetch_jobs as fetch_jobs_tool

        return fetch_jobs_tool(
            skill=skill,
            level=resolved_level,
            limit=limit,
            session_id=session_id,
            readiness_override=readiness_override,
        )

    def get_result(self, session_id: str) -> dict:
        result = get_result(session_id)
        if result:
            return result
        return {"error": "No results found"}

    def send_to_agent(
        self,
        to_agent: str,
        message_type: str,
        session_id: str,
        payload: dict | None = None,
    ):
        message = A2AMessage(
            from_agent="evaluator",
            to_agent=to_agent,
            message_type=message_type,
            session_id=session_id,
            payload=payload or {},
        )
        return a2a_protocol.send_message_sync(message)

    async def handle_a2a_message(self, message: A2AMessage):
        try:
            message.validate()
            if message.to_agent.lower() != "evaluator":
                return {"error": "Message not intended for evaluator"}

            payload = message.payload or {}

            if message.message_type in {A2AProtocol.REQUEST_EVALUATION, A2AProtocol.START_EVALUATION}:
                if payload.get("answers"):
                    return await asyncio.to_thread(
                        self.evaluate_answers,
                        message.session_id,
                        payload.get("skill", ""),
                        payload.get("level", ""),
                        payload.get("answers", []),
                        payload.get("questions"),
                        payload.get("practice_summary"),
                    )
                return await asyncio.to_thread(
                    self.generate_evaluation,
                    message.session_id,
                    payload.get("skill", ""),
                    payload.get("level", "Beginner"),
                    payload.get("question_count", 5),
                )

            if message.message_type == A2AProtocol.FETCH_JOBS:
                from tools.mcp_tools.evaluator import fetch_jobs as fetch_jobs_tool

                return await asyncio.to_thread(
                    self.fetch_jobs,
                    payload.get("skill", ""),
                    payload.get("level", ""),
                    payload.get("limit", 10),
                    message.session_id,
                )

            return {"error": f"Unsupported evaluator message type: {message.message_type}"}
        except Exception as e:
            logger.error(f"Evaluator A2A handling error: {e}")
            return {"error": str(e)}
