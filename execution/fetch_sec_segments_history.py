import requests
import json
import os
import csv
from collections import defaultdict
from datetime import datetime

# Headers required by SEC
HEADERS = {
    "User-Agent": "PersonalInvestmentDashboard/1.0 (bhanu@example.com)"
}

TICKERS = {
    "GOOGL": "0001652044",
    "MELI": "0001099590",
    "NU": "0001691493",
    "NOW": "0001373715"
}

os.makedirs(".tmp", exist_ok=True)

def fetch_company_facts(cik_str):
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_str}.json"
    res = requests.get(url, headers=HEADERS)
    if res.status_code != 200:
        print(f"Failed to fetch {cik_str}: {res.status_code}")
        return None
    return res.json()

def extract_segments(facts_json, ticker):
    print(f"--- Extracting segments for {ticker} ---")
    facts = facts_json.get("facts", {})
    
    # Identify possible revenue tags
    # us-gaap: RevenueFromContractWithCustomerExcludingAssessedTax, Revenues, SalesRevenueNet, etc.
    # or company custom tags
    
    segment_data = defaultdict(lambda: defaultdict(dict)) # quarter -> segment_name -> value
    
    for namespace, concepts in facts.items():
        for concept_name, concept_data in concepts.items():
            # Broad filter for revenue/sales concepts
            if "revenue" not in concept_name.lower() and "sales" not in concept_name.lower():
                continue
                
            units = concept_data.get("units", {})
            for unit, data_points in units.items():
                for dp in data_points:
                    # Look for segment dimensions
                    # Example: 'segment': {'us-gaap:StatementBusinessSegmentsAxis': 'goog:GoogleServicesSegmentMember'}
                    if "segment" not in dp:
                        continue
                        
                    # We want quarterly data, typically frame looks like 'CY2023Q1' or 'CY2023Q1I'
                    # Or we can just use 'end' date and 'val'
                    frame = dp.get("frame", "")
                    if not frame or len(frame) < 6 or "Q" not in frame:
                        continue # Skip annuals or un-framed for simplicity in this script
                    if len(frame) > 8: # skip things like CY2023Q1I
                        continue
                        
                    seg_dict = dp["segment"]
                    # We might have multiple dimensions, just join them
                    seg_name = " | ".join(str(v).split(":")[-1] for v in seg_dict.values())
                    
                    val = dp.get("val")
                    
                    # Store it
                    segment_data[frame][f"{concept_name} - {seg_name}"] = val

    # Convert to CSV
    if not segment_data:
        print(f"No segment data found for {ticker} using default filters. SEC XBRL requires custom mapping.")
        return
        
    quarters = sorted(list(segment_data.keys()))[-20:] # Last 20 quarters
    
    all_segments = set()
    for q in quarters:
        all_segments.update(segment_data[q].keys())
    all_segments = sorted(list(all_segments))
    
    out_file = f".tmp/segments_{ticker}.csv"
    with open(out_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Quarter"] + all_segments)
        for q in quarters:
            row = [q]
            for seg in all_segments:
                val = segment_data[q].get(seg, "")
                if val != "":
                    # format as billions for readability if very large
                    val = round(val / 1e9, 2)
                row.append(val)
            writer.writerow(row)
            
    print(f"Saved 20-quarter segment history for {ticker} to {out_file}")

def main():
    for ticker, cik in TICKERS.items():
        facts = fetch_company_facts(cik)
        if facts:
            extract_segments(facts, ticker)

if __name__ == "__main__":
    main()
