import os
import sys

sys.path.append(os.path.abspath('c:\\Users\\vtvip\\multi-agent'))

from tools.mcp_tools.evaluator import fetch_jobs

def run_test():
    print('Testing fetch_jobs for React (limit=6 to bypass cache)...')
    try:
        result = fetch_jobs(skill='React', level='Beginner', limit=6, session_id='')
        jobs = result.get('jobs', [])
        print('Jobs returned:', len(jobs))
        for j in jobs:
            print(f"- {j.get('title')} @ {j.get('company')} | Location: {j.get('location')}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == '__main__':
    run_test()
