"""
Trinity — REST API Client Example
====================================
Demonstrates how to use Trinity's REST API for CRUD operations.
"""

import requests

BASE_URL = "http://localhost:8100"

# Write memory
resp = requests.post(f"{BASE_URL}/memories", json={
    "content": "User prefers Python 3.12",
    "importance": 0.8,
    "tags": ["python", "preference"]
})
print("Write:", resp.json().get("id", resp.status_code))

# Search memory
resp = requests.get(f"{BASE_URL}/search", params={"q": "python", "top_k": 5})
results = resp.json()
print(f"Search results: {len(results)}")
for r in results:
    print(f"  [{r['score']:.2f}] {r['content']}")

# Get diagnostics
resp = requests.get(f"{BASE_URL}/diagnostics")
print("Diagnostics:", resp.json())
