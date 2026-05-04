import requests
import json

HEADERS = {"User-Agent": "Bhanu Nuthakki (bnuthakki@gmail.com)"}

def reverse_engineer_tag(cik, target_value):
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    res = requests.get(url, headers=HEADERS)
    facts = res.json().get("facts", {})
    
    for ns, concepts in facts.items():
        for concept, data in concepts.items():
            for unit, dps in data.get("units", {}).items():
                for dp in dps:
                    val = dp.get("val")
                    # allow small rounding difference
                    if val is not None and abs(val - target_value) < 100000:
                        print(f"FOUND! Namespace: {ns}, Concept: {concept}")
                        if "segment" in dp:
                            print(f"Segment: {dp['segment']}")
                        print(f"Full DP: {dp}")
                        print("-------------")

# GOOGL Cloud 2025 revenue was 58,705,000,000
print("Searching for GOOGL Cloud Revenue...")
reverse_engineer_tag("0001652044", 58705000000)

# MELI Commerce Brazil product sales was 2,325,000,000 in 2025
print("Searching for MELI Commerce Brazil Product Sales...")
reverse_engineer_tag("0001099590", 2325000000)

# NOW Subscription revenue was 12,883,000,000
print("Searching for NOW Subscription Revenue...")
reverse_engineer_tag("0001373715", 12883000000)
