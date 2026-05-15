"""Quick test Google CSE with the new CX."""
import os
from dotenv import load_dotenv
load_dotenv()
import httpx

api_key = os.getenv("GOOGLE_CUSTOM_SEARCH_API_KEY")
cx = os.getenv("GOOGLE_CUSTOM_SEARCH_CX")
print(f"CX: {cx}")

with httpx.Client(timeout=10) as client:
    resp = client.get("https://www.googleapis.com/customsearch/v1", params={
        "key": api_key,
        "cx": cx,
        "q": 'site:linkedin.com/in "Nubank" "sales"',
        "num": "10",
        "gl": "br",
    })
    print(f"Status: {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        items = data.get("items", [])
        print(f"Resultados: {len(items)}")
        for item in items[:5]:
            print(f"  {item.get('title','')[:60]}")
            print(f"  {item.get('link','')}")
        info = data.get("searchInformation", {})
        print(f"Total estimado: {info.get('totalResults', '?')}")
    else:
        print(f"Erro: {resp.text[:300]}")
