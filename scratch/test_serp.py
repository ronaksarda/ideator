import os
from dotenv import load_dotenv
load_dotenv(".env")
from serpapi import GoogleSearch

key = os.getenv("SERPAPI_API_KEY")
idea = "Ai Tutor who teaches on whiteboard"

# Test 1: strict quotes
s1 = GoogleSearch({"q": f'"{idea}" software alternative', "api_key": key, "num": 3})
r1 = s1.get_dict().get("organic_results", [])
print("Test 1 (strict quotes):", len(r1))

# Test 2: clean keywords
clean = " ".join(idea.split()[:4])
s2 = GoogleSearch({"q": f"{clean} competitor alternative software", "api_key": key, "num": 3})
r2 = s2.get_dict().get("organic_results", [])
print("Test 2 (clean keywords):", len(r2))
for r in r2:
    print(" ->", r.get("title"))
