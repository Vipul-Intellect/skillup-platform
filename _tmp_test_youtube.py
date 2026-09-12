import json
import logging
logging.basicConfig(level=logging.INFO)

from tools.mcp_tools.youtube import search_videos

res = search_videos(
    skill="Python",
    level="Beginner",
    topic="Variables",
    max_results=6
)

for v in res.get("videos", []):
    print(f"Title: {v['title'].encode('ascii', 'ignore').decode('ascii')}")
    print(f"Learning Fit: {v.get('learning_fit_score')}")
    print(f"Concepts: {v.get('concepts_covered')}")
    print("---")
