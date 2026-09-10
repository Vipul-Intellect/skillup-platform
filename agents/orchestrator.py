import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from json import JSONDecodeError
import os
import sys
import threading
import uuid
from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from google import genai
from google.genai import types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from utils.logger import get_logger
from config.settings import settings
from a2a.protocol import A2AMessage, A2AProtocol, a2a_protocol

logger = get_logger(__name__)

# Get absolute path to MCP server
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCP_SERVER_PATH = os.path.join(PROJECT_ROOT, "tools", "mcp_tools", "server.py")


def _child_process_env() -> dict:
    """Explicitly forward Cloud Run env vars to the MCP subprocess."""
    env = os.environ.copy()
    existing_path = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = PROJECT_ROOT if not existing_path else f"{PROJECT_ROOT}{os.pathsep}{existing_path}"
    return env

class OrchestratorAgent:
    def __init__(self):
        try:
            # Gemini SDK client used by the underlying ADK runtime and fallback flows.
            self.client = genai.Client(api_key=settings.GEMINI_API_KEY)
            self.model_id = "gemini-2.5-flash"
            
            # MCP Server Parameters (stdio connection with absolute path)
            self.server_params = StdioServerParameters(
                command=sys.executable,
                args=[MCP_SERVER_PATH],
                env=_child_process_env()
            )
            
            self.loop = asyncio.new_event_loop()
            self._loop_lock = threading.Lock()
            self._stdio_context = None
            self._session_context = None
            self.mcp_session = None
            self.tools = []
            self._persistence_executor = ThreadPoolExecutor(
                max_workers=4,
                thread_name_prefix="agent1-persist",
            )
            self._background_executor = ThreadPoolExecutor(
                max_workers=2,
                thread_name_prefix="agent1-bg",
            )
            self._skill_gap_jobs_lock = threading.Lock()
            self._skill_gap_jobs = {}
            self._session_state_lock = threading.Lock()
            self._session_state = {}
            self.adk_app_name = "skillup_orchestrator"

            # Official ADK architecture for Agent 1:
            # LlmAgent + McpToolset + ADK runner/session management.
            self.adk_connection_params = StdioConnectionParams(
                server_params=self.server_params,
                timeout=float(settings.API_TIMEOUT),
            )
            self.adk_toolset = McpToolset(
                connection_params=self.adk_connection_params,
            )
            self.adk_agent = LlmAgent(
                name="orchestrator",
                model=self.model_id,
                description="Primary SkillUp orchestrator agent coordinating skill discovery and validation workflows.",
                instruction="""You are the primary SkillUp orchestrator agent.

You must use the available MCP tools to complete resume analysis, trending-skill lookup,
skill-gap discovery, and assessment validation tasks. Always prefer tool-grounded answers
over unsupported free-form responses.""",
                tools=[self.adk_toolset],
                generate_content_config=types.GenerateContentConfig(
                    temperature=0.1,
                    top_p=0.9,
                ),
            )
            self.adk_runner = InMemoryRunner(
                agent=self.adk_agent,
                app_name=self.adk_app_name,
            )

            a2a_protocol.register_agent("orchestrator", self)
            
            logger.info("Orchestrator agent initialized with ADK runtime and MCP")
        except Exception as e:
            logger.error(f"Failed to initialize Orchestrator: {e}")
            raise
    
    async def connect_mcp(self):
        """Connect to MCP server via stdio"""
        try:
            await self._ensure_mcp_session()
            logger.info(f"Connected to MCP server with {len(self.tools)} tools")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to MCP server: {e}")
            return False

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
        """Synchronously warm the persistent MCP session during app startup."""
        try:
            self._run_on_loop(self._ensure_mcp_session())
            logger.info("Orchestrator MCP session warmed")
            return True
        except Exception as e:
            logger.warning(f"Orchestrator MCP warmup skipped: {e}")
            return False
    
    async def _call_mcp_tool(self, tool_name: str, arguments: dict):
        """Call MCP tool via stdio connection"""
        try:
            session = await self._ensure_mcp_session()
            result = await session.call_tool(tool_name, arguments)
            
            if result.content:
                for content in result.content:
                    if hasattr(content, 'text'):
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
            logger.error(f"MCP tool call error: {e}")
            await self._reset_mcp_session()
            return {"error": str(e)}
    
    async def _execute_agent_request(self, user_message: str, session_id: str = None):
        """Execute a free-form request through the official ADK runtime."""
        try:
            resolved_session_id = session_id or "orchestrator-default"
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
            import traceback
            logger.error(f"Agent request error: {e}")
            logger.error(traceback.format_exc())
            return json.dumps({"error": str(e)})

    def get_adk_agent(self):
        """Expose the official ADK primary agent for future sub-agent wiring."""
        return self.adk_agent

    def get_adk_runner(self):
        """Expose the ADK runner so future API or A2A flows can execute through ADK."""
        return self.adk_runner
    
    def fetch_trending_skills(self, target_role: str = None):
        """Fetch trending skills via MCP. If target_role provided, returns role-specific skills."""
        arguments = {}
        if target_role:
            arguments["target_role"] = target_role
        return self._run_on_loop(self._call_mcp_tool("fetch_trending_skills", arguments))
    
    def analyze_resume(self, resume_text: str, session_id: str):
        """Analyze resume via MCP"""
        result = self._run_on_loop(
            self._call_mcp_tool(
                "analyze_resume",
                {
                    "resume_text": resume_text,
                    "session_id": session_id,
                },
            )
        )
        self._cache_resume_session_state(session_id, result)
        self._persist_resume_result_async(session_id, result)
        return result

    def analyze_resume_document(self, file_name: str, mime_type: str, file_bytes: bytes, session_id: str):
        """Analyze uploaded resume document via MCP."""
        encoded_file = base64.b64encode(file_bytes).decode("utf-8")
        result = self._run_on_loop(
            self._call_mcp_tool(
                "analyze_resume_document",
                {
                    "file_name": file_name,
                    "mime_type": mime_type,
                    "file_data_base64": encoded_file,
                    "session_id": session_id,
                },
            )
        )
        self._cache_resume_session_state(session_id, result)
        self._persist_resume_result_async(session_id, result)
        return result

    def _cache_resume_session_state(self, session_id: str, result: dict):
        """Keep a fast in-memory copy of the latest resume-derived session state."""
        if not isinstance(result, dict) or result.get("error"):
            return

        payload = {
            "user_skills": result.get("user_skills", []),
            "experience_level": result.get("experience_level", "Beginner"),
            "domain": result.get("domain", "General"),
        }
        self._merge_local_session_state(session_id, payload)

    def _submit_persistence(self, fn, *args, **kwargs):
        """Run Firestore persistence off the user-visible request path."""
        try:
            self._persistence_executor.submit(fn, *args, **kwargs)
        except Exception as e:
            logger.warning(f"Failed to queue persistence work: {e}")

    def _persist_resume_result_async(self, session_id: str, result: dict):
        """Persist extracted resume profile in the main process without blocking."""
        if not isinstance(result, dict) or result.get("error"):
            return

        def _persist():
            try:
                from database.firestore_client import save_session

                save_session(
                    session_id,
                    {
                        "user_skills": result.get("user_skills", []),
                        "experience_level": result.get("experience_level", "Beginner"),
                        "domain": result.get("domain", "General"),
                    },
                )
            except Exception as e:
                logger.warning(f"Failed to persist resume result for {session_id}: {e}")

        self._submit_persistence(_persist)
    
    def _persist_skill_gap_result_async(self, session_id: str, result: dict):
        """Persist skill-gap insights without blocking the API response."""
        if not isinstance(result, dict) or result.get("error"):
            return

        def _persist():
            try:
                from database.firestore_client import save_resume_insights

                save_resume_insights(session_id, result)
            except Exception as e:
                logger.warning(f"Failed to persist skill-gap result for {session_id}: {e}")

        self._submit_persistence(_persist)

    def _persist_assessment_questions_async(
        self,
        session_id: str,
        skill: str,
        declared_level: str,
        result: dict,
    ):
        """Persist assessment state without blocking the request."""
        if not isinstance(result, dict) or result.get("error"):
            return

        self._merge_local_session_state(
            session_id,
            {
                "skill": skill,
                "declared_level": declared_level,
                "assessment_questions": result.get("questions", []),
            },
        )

        def _persist():
            try:
                from database.firestore_client import save_session

                save_session(
                    session_id,
                    {
                        "skill": skill,
                        "declared_level": declared_level,
                        "assessment_questions": result.get("questions", []),
                    },
                )
            except Exception as e:
                logger.warning(f"Failed to persist assessment questions for {session_id}: {e}")

        self._submit_persistence(_persist)

    def _persist_validated_level_async(self, session_id: str, result: dict):
        """Persist validated level without blocking the request."""
        if not isinstance(result, dict) or result.get("error"):
            return

        self._merge_local_session_state(
            session_id,
            {
                "validated_level": result.get("validated_level", "Beginner"),
                "level_confidence": result.get("confidence", 0.5),
            },
        )

        def _persist():
            try:
                from database.firestore_client import save_session

                save_session(
                    session_id,
                    {
                        "validated_level": result.get("validated_level", "Beginner"),
                        "level_confidence": result.get("confidence", 0.5),
                    },
                )
            except Exception as e:
                logger.warning(f"Failed to persist validated level for {session_id}: {e}")

        self._submit_persistence(_persist)

    def compute_skill_gaps(self, session_id: str, target_role: str, user_skills=None):
        """Compute skill gaps via MCP"""
        return self._compute_skill_gaps(session_id, target_role, user_skills=user_skills)

    def _compute_skill_gaps(
        self,
        session_id: str,
        target_role: str,
        user_skills=None,
        force_live_market_profile: bool = False,
    ):
        """Internal skill-gap computation with optional live market refresh enforcement."""
        resolved_skills = user_skills
        if resolved_skills is None:
            from database.firestore_client import get_session

            session = self._get_local_session_state(session_id) or get_session(session_id)
            if not session:
                logger.error(f"Session not found: {session_id}")
                return {"error": "Session not found"}

            resolved_skills = session.get('user_skills', [])
            if not resolved_skills:
                logger.error(f"User skills not found for session: {session_id}")
                return {"error": "User skills not found"}

        result = self._run_on_loop(
            self._call_mcp_tool(
                "find_skill_gaps_with_recommendations",
                {
                    "user_skills": resolved_skills,
                    "target_role": target_role,
                    "session_id": session_id,
                    "force_live_market_profile": force_live_market_profile,
                },
            )
        )
        self._persist_skill_gap_result_async(session_id, result)
        return result

    def start_skill_gap_job(self, session_id: str, target_role: str, user_skills=None):
        """Queue a background live skill-gap computation job."""
        resolved_skills = user_skills
        if resolved_skills is None:
            session = self._get_local_session_state(session_id)
            if session:
                resolved_skills = session.get("user_skills")

        job_id = str(uuid.uuid4())
        job_record = {
            "job_id": job_id,
            "session_id": session_id,
            "target_role": target_role,
            "user_skills": resolved_skills or [],
            "status": "processing",
        }
        self._set_local_skill_gap_job(job_id, job_record)

        def _persist_initial_job():
            try:
                from database.firestore_client import save_skill_gap_job

                save_skill_gap_job(job_id, job_record)
            except Exception as e:
                logger.warning(f"Failed to persist initial skill-gap job {job_id}: {e}")

        self._submit_persistence(_persist_initial_job)

        self._background_executor.submit(
            self._run_skill_gap_job,
            job_id,
            session_id,
            target_role,
            resolved_skills,
        )

        return {
            "job_id": job_id,
            "status": "processing",
            "target_role": target_role,
        }

    def _run_skill_gap_job(self, job_id: str, session_id: str, target_role: str, user_skills=None):
        """Execute a queued skill-gap job and persist its result."""
        from database.firestore_client import save_skill_gap_job

        try:
            result = self._compute_skill_gaps(
                session_id=session_id,
                target_role=target_role,
                user_skills=user_skills,
                force_live_market_profile=True,
            )
            status = "completed" if not result.get("error") else "failed"
            final_record = {
                "job_id": job_id,
                "session_id": session_id,
                "target_role": target_role,
                "user_skills": user_skills or [],
                "status": status,
                "result": result,
            }
            self._set_local_skill_gap_job(job_id, final_record)
            save_skill_gap_job(job_id, final_record)
        except Exception as e:
            logger.error(f"Skill-gap job failed {job_id}: {e}")
            failed_record = {
                "job_id": job_id,
                "session_id": session_id,
                "target_role": target_role,
                "user_skills": user_skills or [],
                "status": "failed",
                "error": str(e),
            }
            self._set_local_skill_gap_job(job_id, failed_record)
            save_skill_gap_job(job_id, failed_record)

    def get_skill_gap_job_status(self, job_id: str):
        """Fetch the current status of an async skill-gap job."""
        from database.firestore_client import get_skill_gap_job

        local_job = self._get_local_skill_gap_job(job_id)
        if local_job:
            return local_job

        job = get_skill_gap_job(job_id)
        if not job:
            return {"error": "Job not found"}
        return job

    def _set_local_skill_gap_job(self, job_id: str, payload: dict):
        with self._skill_gap_jobs_lock:
            self._skill_gap_jobs[job_id] = dict(payload)

    def _get_local_skill_gap_job(self, job_id: str):
        with self._skill_gap_jobs_lock:
            payload = self._skill_gap_jobs.get(job_id)
            if not payload:
                return None
            return dict(payload)

    def _merge_local_session_state(self, session_id: str, payload: dict):
        with self._session_state_lock:
            existing = dict(self._session_state.get(session_id, {}))
            existing.update(payload)
            self._session_state[session_id] = existing

    def _get_local_session_state(self, session_id: str):
        with self._session_state_lock:
            payload = self._session_state.get(session_id)
            if not payload:
                return None
            return dict(payload)
    
    def assess_user_level(self, session_id: str, skill: str, declared_level: str):
        """Generate assessment questions via MCP"""
        result = self._run_on_loop(
            self._call_mcp_tool(
                "generate_assessment_questions",
                {
                    "skill": skill,
                    "declared_level": declared_level,
                    "session_id": session_id,
                },
            )
        )
        self._persist_assessment_questions_async(session_id, skill, declared_level, result)
        return result
    
    def validate_level(
        self,
        session_id: str,
        answers: list,
        questions=None,
        declared_level: str = None,
    ):
        """Validate user level via MCP"""
        arguments = {
            "session_id": session_id,
            "answers": answers,
        }
        if questions is not None:
            arguments["questions"] = questions
        if declared_level is not None:
            arguments["declared_level"] = declared_level

        result = self._run_on_loop(
            self._call_mcp_tool(
                "validate_user_level",
                arguments,
            )
        )
        self._persist_validated_level_async(session_id, result)
        return result

    def send_to_agent(
        self,
        to_agent: str,
        message_type: str,
        session_id: str,
        payload: dict = None
    ):
        """Send an A2A message to another registered agent."""
        message = A2AMessage(
            from_agent="orchestrator",
            to_agent=to_agent,
            message_type=message_type,
            session_id=session_id,
            payload=payload or {}
        )
        return a2a_protocol.send_message_sync(message)

    async def handle_a2a_message(self, message: A2AMessage):
        """Handle A2A messages routed to the orchestrator."""
        try:
            message.validate()

            if message.to_agent.lower() != "orchestrator":
                return {"error": "Message not intended for orchestrator"}

            if message.message_type == A2AProtocol.EVALUATION_COMPLETE:
                if message.payload.get("persist", True):
                    try:
                        from database.firestore_client import save_session

                        save_session(
                            message.session_id,
                            {
                                "last_a2a_event": message.message_type,
                                "evaluation_result": message.payload,
                            },
                        )
                    except Exception as db_error:
                        logger.warning(
                            f"Failed to persist evaluation_complete event: {db_error}"
                        )

                return {
                    "acknowledged": True,
                    "handled_by": "orchestrator",
                    "message_type": message.message_type,
                    "session_id": message.session_id,
                }

            return {
                "acknowledged": False,
                "handled_by": "orchestrator",
                "error": f"Unsupported message type: {message.message_type}",
                "session_id": message.session_id,
            }

        except Exception as e:
            logger.error(f"Orchestrator A2A handling error: {e}")
            return {"error": str(e), "handled_by": "orchestrator"}

