# Learning Agent MCP Server
import sys
import os

# Add project root to path (needed when running as subprocess)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import asyncio
import json
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from utils.logger import get_logger

logger = get_logger(__name__)

# Import tool functions
# Create MCP Server for Learning Agent
app = Server("skillup-learning-tools")

@app.list_tools()
async def list_tools():
    """List all Learning Agent MCP tools"""
    return [
        # YouTube Tools
        Tool(
            name="recommend_topics",
            description="Recommend 2-3 learning topics for a skill and level, excluding already-known topics when provided",
            inputSchema={
                "type": "object",
                "properties": {
                    "skill": {"type": "string", "description": "Skill to learn"},
                    "level": {"type": "string", "enum": ["Beginner", "Intermediate", "Advanced"], "description": "User skill level"},
                    "count": {"type": "integer", "description": "Number of topics to recommend (max 3)"},
                    "exclude_topics": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Topics to avoid because the user already knows them"
                    }
                },
                "required": ["skill", "level"]
            }
        ),
        Tool(
            name="search_videos",
            description="Search YouTube for tutorial videos using 6 curated channels and 6 live YouTube results",
            inputSchema={
                "type": "object",
                "properties": {
                    "skill": {"type": "string", "description": "Skill to search tutorials for"},
                    "level": {"type": "string", "enum": ["Beginner", "Intermediate", "Advanced"], "description": "User skill level"},
                    "topic": {"type": "string", "description": "Specific topic within the skill"},
                    "preferred_duration": {"type": "string", "enum": ["10 min", "40 min", "60 min", "2 hours"], "description": "Preferred video duration"},
                    "max_results": {"type": "integer", "description": "Maximum videos to return (default 12)"}
                },
                "required": ["skill", "level"]
            }
        ),
        
        # Code Execution Tools
        Tool(
            name="execute_code",
            description="Execute code in various programming languages using Piston API",
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Source code to execute"},
                    "language": {"type": "string", "description": "Programming language (e.g., python, javascript, ruby)"},
                    "stdin": {"type": "string", "description": "Standard input for the code"},
                    "context": {
                        "type": "object",
                        "description": "Learning context (skill, topic, level, task)",
                        "additionalProperties": True
                    }
                },
                "required": ["code", "language"]
            }
        ),
        Tool(
            name="validate_code",
            description="Validate code syntax without executing",
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Source code to validate"},
                    "language": {"type": "string", "description": "Programming language"}
                },
                "required": ["code", "language"]
            }
        ),
        Tool(
            name="get_execution_config",
            description="Get Monaco/editor runtime config and supported execution languages",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        ),
        
        # Socratic Assistant Tools
        Tool(
            name="generate_practice_set",
            description="Generate 3-4 practice questions and 1 mini-lab for a selected skill topic and language",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "User session ID"},
                    "skill": {"type": "string", "description": "Current skill"},
                    "topic": {"type": "string", "description": "Specific topic"},
                    "level": {"type": "string", "enum": ["Beginner", "Intermediate", "Advanced"], "description": "User level"},
                    "language": {"type": "string", "description": "Programming language for the mini-lab"},
                    "video_context": {"type": "object", "description": "Optional selected-video context to make the practice more specific"}
                },
                "required": ["session_id", "skill", "topic", "level"]
            }
        ),
        Tool(
            name="evaluate_practice_answers",
            description="Evaluate learner answers for the active practice pack and decide whether they are acceptable",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "User session ID"},
                    "skill": {"type": "string", "description": "Current skill"},
                    "topic": {"type": "string", "description": "Specific topic"},
                    "answers": {"type": "array", "items": {"type": "string"}, "description": "Learner answers in order"}
                },
                "required": ["session_id", "skill", "topic", "answers"]
            }
        ),
        Tool(
            name="get_socratic_hint",
            description="Generate Socratic hints - max 2 lines code, always ends with question, tracks weak topics",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "User session ID"},
                    "skill": {"type": "string", "description": "Current skill"},
                    "topic": {"type": "string", "description": "Specific topic"},
                    "level": {"type": "string", "enum": ["Beginner", "Intermediate", "Advanced"], "description": "User level"},
                    "code": {"type": "string", "description": "User's code"},
                    "error": {"type": "string", "description": "Error message"},
                    "hint_level": {"type": "integer", "enum": [1, 2, 3], "description": "1=concept, 2=approach, 3=structure"}
                },
                "required": ["session_id", "skill", "topic", "level"]
            }
        ),
        Tool(
            name="get_weak_topics",
            description="Analyze hint usage to identify topics needing more practice",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "User session ID"}
                },
                "required": ["session_id"]
            }
        ),
        Tool(
            name="explain_concept",
            description="Explain a programming concept at appropriate level with code example",
            inputSchema={
                "type": "object",
                "properties": {
                    "skill": {"type": "string", "description": "Skill being learned"},
                    "topic": {"type": "string", "description": "Concept to explain"},
                    "level": {"type": "string", "enum": ["Beginner", "Intermediate", "Advanced"], "description": "User level"}
                },
                "required": ["skill", "topic", "level"]
            }
        ),
        
        # Schedule Tools
        Tool(
            name="generate_schedule",
            description="Generate personalized learning schedule with video/coding/revision time allocation",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "User session ID"},
                    "skill": {"type": "string", "description": "Skill to learn"},
                    "level": {"type": "string", "enum": ["Beginner", "Intermediate", "Advanced"], "description": "User level"},
                    "mode": {"type": "string", "enum": ["balanced", "deep_learning", "fast_track", "practice_focused"], "description": "Schedule mode"},
                    "daily_time": {"type": "integer", "description": "Daily time available in minutes"}
                },
                "required": ["session_id", "skill", "level", "mode", "daily_time"]
            }
        ),
        Tool(
            name="get_schedule",
            description="Get current learning schedule for session",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "User session ID"}
                },
                "required": ["session_id"]
            }
        ),
        Tool(
            name="update_schedule_progress",
            description="Update schedule progress - mark day as completed",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "User session ID"},
                    "day": {"type": "integer", "description": "Day number"},
                    "completed": {"type": "boolean", "description": "Whether day is completed"}
                },
                "required": ["session_id", "day"]
            }
        )
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict):
    """Execute Learning Agent MCP tool"""
    logger.info(f"Learning MCP Tool called: {name}")
    
    try:
        result = None
        
        # YouTube Tools
        if name == "recommend_topics":
            from tools.mcp_tools import youtube

            result = youtube.recommend_topics(
                skill=arguments.get("skill", ""),
                level=arguments.get("level", "Beginner"),
                count=arguments.get("count", 3),
                exclude_topics=arguments.get("exclude_topics", []),
            )
        elif name == "search_videos":
            from tools.mcp_tools import youtube

            result = youtube.search_videos(
                skill=arguments.get("skill", ""),
                level=arguments.get("level", "Beginner"),
                max_results=arguments.get("max_results", 12),
                topic=arguments.get("topic", ""),
                preferred_duration=arguments.get("preferred_duration", ""),
            )
        
        # Code Execution Tools
        elif name == "execute_code":
            from tools.mcp_tools import code_executor

            result = code_executor.execute_code(
                code=arguments.get("code", ""),
                language=arguments.get("language", "python"),
                stdin=arguments.get("stdin", ""),
                context=arguments.get("context", {})
            )
        elif name == "validate_code":
            from tools.mcp_tools import code_executor

            result = code_executor.validate_code(
                code=arguments.get("code", ""),
                language=arguments.get("language", "python")
            )
        elif name == "get_execution_config":
            from tools.mcp_tools import code_executor

            result = code_executor.get_execution_config()

        # Socratic Tools
        elif name == "generate_practice_set":
            from tools.mcp_tools import socratic

            result = socratic.generate_practice_set(
                session_id=arguments.get("session_id", ""),
                skill=arguments.get("skill", ""),
                topic=arguments.get("topic", ""),
                level=arguments.get("level", "Beginner"),
                language=arguments.get("language", "python"),
                video_context=arguments.get("video_context", {}),
            )
        elif name == "get_socratic_hint":
            from tools.mcp_tools import socratic

            result = socratic.get_socratic_hint(
                session_id=arguments.get("session_id", ""),
                skill=arguments.get("skill", ""),
                topic=arguments.get("topic", ""),
                level=arguments.get("level", "Beginner"),
                code=arguments.get("code", ""),
                error=arguments.get("error", ""),
                hint_level=arguments.get("hint_level", 1)
            )
        elif name == "evaluate_practice_answers":
            from tools.mcp_tools import socratic

            result = socratic.evaluate_practice_answers(
                session_id=arguments.get("session_id", ""),
                skill=arguments.get("skill", ""),
                topic=arguments.get("topic", ""),
                answers=arguments.get("answers", []),
            )
        elif name == "get_weak_topics":
            from tools.mcp_tools import socratic

            result = socratic.get_weak_topics(
                session_id=arguments.get("session_id", "")
            )
        elif name == "explain_concept":
            from tools.mcp_tools import socratic

            result = socratic.explain_concept(
                skill=arguments.get("skill", ""),
                topic=arguments.get("topic", ""),
                level=arguments.get("level", "Beginner")
            )
        
        # Schedule Tools
        elif name == "generate_schedule":
            from tools.mcp_tools import schedule

            result = schedule.generate_schedule(
                session_id=arguments.get("session_id", ""),
                skill=arguments.get("skill", ""),
                level=arguments.get("level", "Beginner"),
                mode=arguments.get("mode", "balanced"),
                daily_time=arguments.get("daily_time", 60)
            )
        elif name == "get_schedule":
            from tools.mcp_tools import schedule

            result = schedule.get_schedule(
                session_id=arguments.get("session_id", "")
            )
        elif name == "update_schedule_progress":
            from tools.mcp_tools import schedule

            result = schedule.update_schedule_progress(
                session_id=arguments.get("session_id", ""),
                day=arguments.get("day", 1),
                completed=arguments.get("completed", True)
            )
        
        else:
            result = {"error": f"Unknown tool: {name}"}
        
        logger.info(f"Learning MCP Tool completed: {name}")
        return [TextContent(type="text", text=json.dumps(result))]
        
    except Exception as e:
        logger.error(f"Learning MCP Tool error: {name} - {e}")
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

async def run_server():
    """Run the Learning Agent MCP server via stdio"""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())

if __name__ == "__main__":
    logger.info("Starting Learning Agent MCP Server via stdio...")
    asyncio.run(run_server())
