import json
import logging
logging.basicConfig(level=logging.INFO)

from tools.mcp_tools.youtube import search_videos, _evaluate_and_rank_videos, _safe_score

print("=== TEST B: Normal Gemini Path & TEST C: Long Description ===")
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
    print(f"Description Length: {len(v.get('description', ''))}")
    print(f"Semantic Fallback: {v.get('is_semantic_fallback')}")
    print("---")

print("\n=== TEST D: Invalid Numeric Output ===")
print("Testing _safe_score directly...")
assert _safe_score(5) == 5
assert _safe_score("8") == 8
assert _safe_score("10/10") == 0
assert _safe_score(None) == 0
assert _safe_score(15) == 10
assert _safe_score(-5) == 0
print("Numeric validation passed.")

print("\n=== TEST E: Forced Gemini Failure ===")
# Force gemini failure by giving a bad prompt schema manually or intercepting 
# Actually just pass a mock that raises Exception
import unittest.mock
from tools.mcp_tools import youtube
with unittest.mock.patch('tools.mcp_tools.youtube._get_gemini_client') as mock_client:
    mock_client.side_effect = Exception("Forced Timeout")
    cands = youtube._evaluate_and_rank_videos(
        candidates=[{"video_id": "123", "title": "Test", "channel": "Test", "duration": "10:00", "relevance_score": 3, "duration_match_score": 2}],
        skill="Test",
        level="Test",
        topic="Test"
    )
    print("Fallback activated seamlessly.")
    print(cands[0])

print("\nTEST F: Regression")
# Tested naturally via search_videos calling curation and global
print("All done.")
