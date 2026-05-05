import requests
import json
import os

HEADERS = {"User-Agent": "PersonalInvestmentDashboard/1.0 (bhanu@example.com)"}
TICKERS = {"GOOGL": "0001652044", "MELI": "0001099590", "NU": "0001691493", "NOW": "0001373715"}

def find_segment_tags(cik_str, keywords):
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_str}.json"
    res = requests.get(url, headers=HEADERS)
    if res.status_code != 200: return
    
    facts = res.json().get("facts", {})
    found_concepts = set()
    for namespace, concepts in facts.items():
        for concept_name, concept_data in concepts.items():
            name_lower = concept_name.lower()
            if any(k in name_lower for k in keywords):
                # Also check if it's monetary
                if "usd" in concept_data.get("units", {}):
                    found_concepts.add(f"{namespace}:{concept_name}")
                    
            # Check dimensions inside data points
            for unit, data_points in concept_data.get("units", {}).items():
                for dp in data_points:
                    if "segment" in dp:
                        for dim, mem in dp["segment"].items():
                            if any(k in str(mem).lower() for k in keywords):
                                found_concepts.add(f"{namespace}:{concept_name} [{dim}={mem}]")
                                
    return found_concepts

print("GOOGL Concepts:", find_segment_tags(TICKERS["GOOGL"], ["revenue", "sales", "cloud", "youtube", "network", "subscription", "segment"]))
print("MELI Concepts:", find_segment_tags(TICKERS["MELI"], ["revenue", "sales", "commerce", "fintech", "mercadopago", "segment"]))
print("NOW Concepts:", find_segment_tags(TICKERS["NOW"], ["revenue", "sales", "subscription", "professional", "segment"]))
