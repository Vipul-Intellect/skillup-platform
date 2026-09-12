# SkillUp Agent

> Google GenAI APAC Academy Grand Hackathon | Track 2: Multi-Agent Systems

SkillUp Agent is an AI-powered skill development platform that takes a learner from profile discovery to guided learning and final readiness evaluation. It is built around three coordinated Google ADK agents, MCP tool servers, Firestore-backed state, and a separate code execution service deployed on AWS ECS (Express Mode).

## Live demo

- App: AWS ECS Deployment (URL pending)
- Repository: [https://github.com/Vipul-Intellect/Skillyfy-Agent](https://github.com/Vipul-Intellect/Skillyfy-Agent)

## What the product does

SkillUp Agent connects the full learning journey in one workflow:

```text
Resume or direct input
  -> trending skills and market-aware gap analysis
  -> level validation
  -> personalized topics, videos, practice, and code labs
  -> final evaluation and readiness result
  -> relevant job recommendations
```

The product is designed to solve a practical problem: most platforms handle only one part of the journey. Resume tools show current profile, learning platforms suggest content, coding sites provide isolated practice, and job portals list openings. SkillUp Agent connects those steps into one guided system.

## Core capabilities

- Resume upload and direct skill input
- Trending skill discovery and market-aware skill-gap analysis
- Level assessment and validation
- Personalized topic recommendations
- Curated and live YouTube learning resources
- Practice questions and mini-labs
- In-browser code editing, validation, and execution
- Socratic hinting instead of full-answer dumping
- Learning schedule generation and progress tracking
- Final readiness evaluation and saved result retrieval
- Live job recommendations

## Architecture

### High-level design

```text
Browser UI
  -> Flask API (AWS ECS)
    -> Agent 1: Orchestrator
    -> Agent 2: Learning
    -> Agent 3: Evaluator
      -> MCP tool servers (stdio)
      -> Firestore / Firebase
      -> Internal code execution service (AWS ECS)
```

### Agent roles

#### Agent 1 - Orchestrator
File: `agents/orchestrator.py`

- Accepts resume or direct input
- Fetches trending skills
- Compares learner profile with target-role demand
- Starts and tracks skill-gap analysis jobs
- Generates and validates level-assessment questions

#### Agent 2 - Learning
File: `agents/learning_agent.py`

- Recommends topics based on skill and level
- Fetches curated and live learning videos
- Generates practice packs and mini-labs
- Supports code validation and execution
- Delivers Socratic hints
- Generates schedules and tracks progress

#### Agent 3 - Evaluator
File: `agents/evaluator_agent.py`

- Generates final evaluation packs
- Scores learner answers
- Stores readiness results
- Fetches relevant job recommendations
- Restores saved evaluation results

### Coordination model

- Google ADK powers the three agents
- MCP tool servers provide structured tool access over stdio
- A2A protocol files in `a2a/` support agent-to-agent coordination
- Firestore preserves workflow state across the full learner journey

## Tech stack

| Technology | Purpose |
| --- | --- |
| Google ADK | Agent runtime and orchestration |
| Gemini 3.6 Flash | Reasoning, generation, scoring, and parsing |
| MCP | Structured tool integration |
| A2A protocol | Agent registration and message routing |
| Flask | Main API and server-rendered web app |
| Firestore / Firebase | Sessions, progress, results, cache, and async job state |
| AWS ECS | Main app deployment and executor deployment (Express Mode) |
| YouTube Data API | Learning video retrieval |
| RapidAPI JSearch | Job recommendation source |
| Piston-based executor | Sandboxed multi-language code execution |
| Monaco Editor | In-browser coding experience |

## Project structure

```text
multi-agent/
|-- agents/
|   |-- orchestrator.py
|   |-- learning_agent.py
|   `-- evaluator_agent.py
|-- api/
|   `-- flask_app.py
|-- a2a/
|   |-- protocol.py
|   `-- registry.py
|-- database/
|   `-- firestore_client.py
|-- executor_service/
|-- tools/
|   `-- mcp_tools/
|       |-- server.py
|       |-- learning_server.py
|       |-- evaluator_server.py
|       |-- trending.py
|       |-- resume.py
|       |-- assessment.py
|       |-- youtube.py
|       |-- socratic.py
|       |-- schedule.py
|       |-- evaluator.py
|       `-- code_executor.py
|-- templates/
|   `-- index.html
|-- static/
|-- config/
|   `-- settings.py
`-- README.md
```

## API surface

### Session and health

- `GET /health`
- `POST /api/session/start`

### Agent 1 endpoints

- `GET|POST /api/trending-skills`
- `POST /api/analyze-resume`
- `POST /api/skill-gaps`
- `POST /api/skill-gaps/start`
- `GET /api/skill-gaps/<job_id>`
- `POST /api/assess-level`
- `POST /api/validate-level`

### Agent 2 endpoints

- `POST /api/topics`
- `POST /api/videos`
- `POST /api/practice`
- `POST /api/practice/evaluate`
- `POST /api/hint`
- `GET /api/execution-config`
- `POST /api/validate-code`
- `POST /api/execute-code`
- `POST /api/schedule`
- `GET /api/schedule/<session_id>`
- `POST /api/schedule/progress`

### Agent 3 endpoints

- `POST /api/evaluate`
- `POST /api/jobs`
- `GET /api/results/<session_id>`

## Firestore collections

The app persists workflow state in Firestore through `database/firestore_client.py`.

- `sessions` - learner session state and selections
- `progress` - learning progress and hint/practice metadata
- `results` - final evaluation outputs
- `resume_insights` - extracted and derived learner insights
- `role_profiles` - cached target-role profiles
- `cache` - general cached responses
- `skill_gap_jobs` - async skill-gap job tracking

## Local setup

### Prerequisites

- Python 3.9+
- Google Cloud project with Firestore enabled
- API keys for Gemini, YouTube Data API, and RapidAPI
- Application Default Credentials for Google Cloud

### Install

```bash
git clone https://github.com/Vipul-Intellect/Skillyfy-Agent.git
cd multi-agent
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
# Fill in the required API keys
gcloud auth application-default login
```

### Run locally

```bash
python api/flask_app.py
```

### Deploy to AWS ECS

The application and executor services are deployed to AWS ECS. Docker images are built and pushed to Amazon ECR.
Refer to `.github/workflows/ecr-executor-test.yml` for the CI/CD pipeline details.

```bash
aws ecr get-login-password --region ap-south-1 | docker login --username AWS --password-stdin <AWS_ACCOUNT_ID>.dkr.ecr.ap-south-1.amazonaws.com
docker build -t skillup-executor ./executor_service
docker tag skillup-executor:latest <AWS_ACCOUNT_ID>.dkr.ecr.ap-south-1.amazonaws.com/skillup-executor:latest
docker push <AWS_ACCOUNT_ID>.dkr.ecr.ap-south-1.amazonaws.com/skillup-executor:latest
```

## Notes

- The main user flow is API-driven through the Flask app
- Agent coordination exists through the A2A layer, while tool execution flows through MCP
- The executor service is separated from the main app so code execution can be isolated from UI and orchestration concerns

## License

MIT
