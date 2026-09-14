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
from database.firestore_client import get_session, save_progress, save_session
from utils.gemini_client_pool import PooledGemini
from utils.logger import get_logger

logger = get_logger(__name__)

# Get absolute path to MCP server
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEARNING_SERVER_PATH = os.path.join(PROJECT_ROOT, "tools", "mcp_tools", "learning_server.py")


def _child_process_env() -> dict:
    """Explicitly forward Cloud Run env vars to the MCP subprocess."""
    env = os.environ.copy()
    existing_path = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = PROJECT_ROOT if not existing_path else f"{PROJECT_ROOT}{os.pathsep}{existing_path}"
    return env


class LearningAgent:
    """
    Learning Agent handles:
    - Video curation
    - Code execution and validation
    - Socratic hints
    - Schedule generation and progress tracking
    """

    def __init__(self):
        try:
            self.model_id = "gemini-3.6-flash"

            self.server_params = StdioServerParameters(
                command=sys.executable,
                args=[LEARNING_SERVER_PATH],
                env=_child_process_env(),
            )

            self.loop = asyncio.new_event_loop()
            self._loop_lock = threading.Lock()
            self._stdio_context = None
            self._session_context = None
            self.mcp_session = None
            self.tools = []
            self.adk_app_name = "skillup_learning"

            self.adk_connection_params = StdioConnectionParams(
                server_params=self.server_params,
                timeout=float(settings.API_TIMEOUT),
            )
            self.adk_toolset = McpToolset(
                connection_params=self.adk_connection_params,
            )
            self.adk_agent = LlmAgent(
                name="learning",
                model=PooledGemini(model=self.model_id),
                description="SkillUp learning agent for videos, practice, hints, and schedules.",
                instruction="""You are the SkillUp learning agent.

Use the available MCP tools to retrieve videos, validate and execute code, generate learning hints,
and manage schedules. Prefer tool-grounded responses and keep outputs structured and concise.""",
                tools=[self.adk_toolset],
                generate_content_config=types.GenerateContentConfig(
                ),
            )
            self.adk_runner = InMemoryRunner(
                agent=self.adk_agent,
                app_name=self.adk_app_name,
            )

            a2a_protocol.register_agent("learning", self)
            logger.info("Learning Agent initialized with ADK runtime and MCP")
        except Exception as e:
            logger.error(f"Failed to initialize Learning Agent: {e}")
            raise

    async def _ensure_mcp_session(self):
        """Create and initialize a persistent MCP session if needed."""
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
        """Reset the persistent MCP session after a transport failure."""
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
        """Run a coroutine on the agent's dedicated event loop."""
        with self._loop_lock:
            return self.loop.run_until_complete(coro)

    def warm_mcp(self):
        """Synchronously warm the persistent MCP session."""
        try:
            self._run_on_loop(self._ensure_mcp_session())
            logger.info("Learning MCP session warmed")
            return True
        except Exception as e:
            logger.warning(f"Learning MCP warmup skipped: {e}")
            return False

    async def _call_mcp_tool(self, tool_name: str, arguments: dict):
        """Call an MCP tool through the persistent stdio session."""
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
            logger.error(f"Learning MCP tool call error: {tool_name} - {e}")
            await self._reset_mcp_session()
            return {"error": str(e)}

    async def _execute_agent_request(self, user_message: str, session_id: str = None):
        """Execute a free-form request through the official ADK runtime."""
        try:
            resolved_session_id = session_id or "learning-default"
            session_service = self.adk_runner.session_service
            existing_session = await session_service.get_session(
                app_name=self.adk_app_name,
                user_id=resolved_session_id,
                session_id=resolved_session_id,
            )
            if existing_session is None:
                await session_service.create_session(
                    app_name=self.adk_app_name,
                    user_id=resolved_session_id,
                    session_id=resolved_session_id,
                )

            user_content = types.UserContent(
                parts=[types.Part.from_text(text=user_message)]
            )

            final_text = ""
            for event in self.adk_runner.run(
                user_id=resolved_session_id,
                session_id=resolved_session_id,
                new_message=user_content,
            ):
                if not getattr(event, "content", None):
                    continue
                parts = getattr(event.content, "parts", []) or []
                text_parts = [part.text for part in parts if getattr(part, "text", None)]
                if text_parts:
                    final_text = "\n".join(text_parts).strip()
                if event.is_final_response() and final_text:
                    break

            if final_text:
                return final_text

            return json.dumps({"error": "No response from ADK agent"})
        except Exception as e:
            logger.error(f"Learning Agent request error: {e}")
            return json.dumps({"error": str(e)})

    def get_adk_agent(self):
        """Expose the official ADK learning agent."""
        return self.adk_agent

    def get_adk_runner(self):
        """Expose the ADK runner for future API/A2A flows."""
        return self.adk_runner

    # ================== VIDEO METHODS ==================

    def recommend_topics(
        self,
        skill: str,
        level: str,
        count: int = 3,
        exclude_topics: list | None = None,
    ) -> dict:
        """Recommend 2-3 learning topics and replace known topics when requested."""
        return self._run_on_loop(
            self._call_mcp_tool(
                "recommend_topics",
                {
                    "skill": skill,
                    "level": level,
                    "count": count,
                    "exclude_topics": exclude_topics or [],
                },
            )
        )

    def search_videos(
        self,
        skill: str,
        level: str,
        max_results: int = 12,
        topic: str = "",
        preferred_duration: str = "",
    ) -> dict:
        """Search videos for a skill."""
        return self._run_on_loop(
            self._call_mcp_tool(
                "search_videos",
                {
                    "skill": skill,
                    "level": level,
                    "max_results": max_results,
                    "topic": topic,
                    "preferred_duration": preferred_duration,
                },
            )
        )

    # ================== CODE EXECUTION METHODS ==================

    def execute_code(self, code: str, language: str, stdin: str = "", context: dict = None) -> dict:
        """Execute user code."""
        return self._run_on_loop(
            self._call_mcp_tool(
                "execute_code",
                {
                    "code": code,
                    "language": language,
                    "stdin": stdin,
                    "context": context or {},
                },
            )
        )

    def get_execution_config(self) -> dict:
        """Get runtime/editor config for the coding environment."""
        return self._run_on_loop(
            self._call_mcp_tool(
                "get_execution_config",
                {},
            )
        )

    def validate_code(self, code: str, language: str) -> dict:
        """Validate code syntax."""
        return self._run_on_loop(
            self._call_mcp_tool(
                "validate_code",
                {
                    "code": code,
                    "language": language,
                },
            )
        )

    # ================== SOCRATIC ASSISTANT METHODS ==================

    def generate_practice(
        self,
        session_id: str,
        skill: str,
        topic: str,
        level: str,
        language: str = "python",
        video_context: dict | None = None,
    ) -> dict:
        """Generate 3-4 questions and 1 mini-lab for a topic."""
        return self._run_on_loop(
            self._call_mcp_tool(
                "generate_practice_set",
                {
                    "session_id": session_id,
                    "skill": skill,
                    "topic": topic,
                    "level": level,
                    "language": language,
                    "video_context": video_context or {},
                },
            )
        )

    def evaluate_practice_answers(
        self,
        session_id: str,
        skill: str,
        topic: str,
        answers: list,
    ) -> dict:
        """Evaluate written answers for the active practice pack."""
        return self._run_on_loop(
            self._call_mcp_tool(
                "evaluate_practice_answers",
                {
                    "session_id": session_id,
                    "skill": skill,
                    "topic": topic,
                    "answers": answers,
                },
            )
        )

    def get_hint(
        self,
        session_id: str,
        skill: str,
        topic: str,
        level: str,
        code: str = "",
        error: str = "",
        hint_level: int = 1,
    ) -> dict:
        """Get Socratic hint."""
        return self._run_on_loop(
            self._call_mcp_tool(
                "get_socratic_hint",
                {
                    "session_id": session_id,
                    "skill": skill,
                    "topic": topic,
                    "level": level,
                    "code": code,
                    "error": error,
                    "hint_level": hint_level,
                },
            )
        )

    def get_weak_topics(self, session_id: str) -> dict:
        """Get weak topics based on hint usage."""
        return self._run_on_loop(
            self._call_mcp_tool(
                "get_weak_topics",
                {"session_id": session_id},
            )
        )

    def explain_concept(self, skill: str, topic: str, level: str) -> dict:
        """Explain a concept."""
        return self._run_on_loop(
            self._call_mcp_tool(
                "explain_concept",
                {
                    "skill": skill,
                    "topic": topic,
                    "level": level,
                },
            )
        )

    # ================== SCHEDULE METHODS ==================

    def generate_schedule(
        self,
        session_id: str,
        skill: str,
        level: str,
        mode: str,
        daily_time: int,
    ) -> dict:
        """Generate learning schedule."""
        result = self._run_on_loop(
            self._call_mcp_tool(
                "generate_schedule",
                {
                    "session_id": session_id,
                    "skill": skill,
                    "level": level,
                    "mode": mode,
                    "daily_time": daily_time,
                },
            )
        )
        if isinstance(result, dict) and not result.get("error"):
            saved = save_session(session_id, {"schedule": result})
            if not saved:
                return {"error": "Failed to persist generated schedule"}
        return result

    def get_schedule(self, session_id: str) -> dict:
        """Get current schedule."""
        session_data = get_session(session_id)
        if not session_data:
            return {"error": "Session not found"}

        schedule = session_data.get("schedule", {})
        if not schedule:
            return {"schedule_enabled": False}
        return schedule

    def update_progress(self, session_id: str, day: int, completed: bool = True) -> dict:
        """Update schedule progress."""
        session_data = get_session(session_id)
        if not session_data:
            return {"error": "Session not found"}

        schedule = session_data.get("schedule", {})
        if not schedule:
            return {"error": "No schedule found"}

        for plan in schedule.get("daily_plan", []):
            if plan.get("day") == day:
                plan["completed"] = completed
                break

        completed_days = sum(1 for plan in schedule.get("daily_plan", []) if plan.get("completed"))
        total_days = len(schedule.get("daily_plan", []))
        progress = int((completed_days / total_days) * 100) if total_days > 0 else 0

        schedule["progress_percentage"] = progress
        schedule["current_day"] = min(day + 1 if completed else day, total_days or day)

        saved = save_session(session_id, {"schedule": schedule})
        if not saved:
            return {"error": "Failed to persist schedule progress"}

        return {
            "day_completed": day,
            "progress_percentage": progress,
            "current_day": schedule["current_day"],
            "total_days": total_days,
        }

    # ================== PROGRESS TRACKING ==================

    def save_learning_progress(
        self,
        session_id: str,
        skill: str,
        topic: str,
        videos_watched: int,
        exercises_completed: int,
        hints_used: int,
    ) -> dict:
        """Save learning progress to Firestore."""
        try:
            progress_data = {
                "skill": skill,
                "topic": topic,
                "videos_watched": videos_watched,
                "exercises_completed": exercises_completed,
                "hints_used": hints_used,
            }
            save_progress(session_id, progress_data)
            return {
                "success": True,
                "session_id": session_id,
                "progress": progress_data,
            }
        except Exception as e:
            logger.error(f"Error saving progress: {e}")
            return {"error": str(e)}

    # ================== A2A METHODS ==================

    def send_to_agent(
        self,
        to_agent: str,
        message_type: str,
        session_id: str,
        payload: dict = None,
    ):
        """Send an A2A message to another registered agent."""
        message = A2AMessage(
            from_agent="learning",
            to_agent=to_agent,
            message_type=message_type,
            session_id=session_id,
            payload=payload or {},
        )
        return a2a_protocol.send_message_sync(message)

    async def handle_a2a_message(self, message: A2AMessage):
        """Handle A2A messages routed to the learning agent."""
        try:
            message.validate()

            if message.to_agent.lower() != "learning":
                return {"error": "Message not intended for learning"}

            if message.message_type == A2AProtocol.GET_VIDEOS:
                payload = message.payload or {}
                return await asyncio.to_thread(
                    self.search_videos,
                    payload.get("skill", ""),
                    payload.get("level", "Beginner"),
                    payload.get("max_results", 5),
                    payload.get("topic", ""),
                    payload.get("preferred_duration", ""),
                )

            if message.message_type == A2AProtocol.START_LEARNING:
                return {
                    "acknowledged": True,
                    "handled_by": "learning",
                    "message_type": message.message_type,
                    "session_id": message.session_id,
                }

            return {
                "acknowledged": False,
                "handled_by": "learning",
                "error": f"Unsupported message type: {message.message_type}",
                "session_id": message.session_id,
            }
        except Exception as e:
            logger.error(f"Learning A2A handling error: {e}")
            return {"error": str(e), "handled_by": "learning"}
