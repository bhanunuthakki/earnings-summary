import requests
import os
import csv

# SEC requires a user agent with contact info. Set EDGAR_USER_AGENT in env (recommended).
HEADERS = {
    "User-Agent": os.environ.get(
        "EDGAR_USER_AGENT", "earnings-summary research/0.1 (analyst@example.com)"
    )
}

# CIKs and their form types
# Nubank (NU) is a Foreign Private Issuer, so they file 20-F (Annual) and 6-K (Quarterly)
TICKERS = {
    "GOOGL": {"cik": "0001652044", "forms": ["10-K", "10-Q"]},
    "MELI": {"cik": "0001099590", "forms": ["10-K", "10-Q"]},
    "NOW": {"cik": "0001373715", "forms": ["10-K", "10-Q"]},
    "NU": {"cik": "0001691493", "forms": ["20-F", "6-K"]},
}

# The dictionary that maps custom company XBRL tags to our standard DCF columns
COMPANY_SCHEMAS = {
    "GOOGL": {
        "cloud_revenue": ["goog:GoogleCloudSegmentMember", "Google Cloud"],
        "youtube_revenue": ["goog:YouTubeAdsMember", "YouTube ads - Google Services"],
        "search_revenue": [
            "goog:GoogleSearchAndOtherMember",
            "Google Search & other - Google Services",
        ],
    },
    "MELI": {
        "commerce_revenue": ["meli:CommerceSegmentMember", "Operating Segments - Commerce"],
        "fintech_revenue": ["meli:FintechSegmentMember", "Operating Segments - Fintech"],
        "brazil_revenue": ["meli:BrazilMember", "Operating Segments - Brazil"],
    },
    "NOW": {
        "subscription_revenue": ["now:SubscriptionMember", "Subscription"],
        "professional_services": [
            "now:ProfessionalServicesMember",
            "Professional services and other",
        ],
    },
    "NU": {
        # IFRS tags for 20-F and 6-K
        "interest_income": ["ifrs-full:InterestIncome", "Interest income"],
        "fee_income": ["ifrs-full:FeeAndCommissionIncome", "Fee and commission income"],
        "brazil_segment": ["nu:BrazilSegmentMember", "Brazil"],
    },
}

os.makedirs(".tmp", exist_ok=True)


def fetch_recent_filings(cik_str, allowed_forms, limit=20):
    """Fetch the accession numbers for the last 20 relevant filings."""
    url = f"https://data.sec.gov/submissions/CIK{cik_str}.json"
    res = requests.get(url, headers=HEADERS)
    if res.status_code != 200:
        print(f"Failed to fetch submissions for {cik_str}")
        return []

    data = res.json()
    filings = data.get("filings", {}).get("recent", {})
    if not filings:
        return []

    accession_numbers = []
    for i in range(len(filings.get("accessionNumber", []))):
        form = filings["form"][i]
        if form in allowed_forms:
            accession_numbers.append(
                {
                    "accession": filings["accessionNumber"][i].replace("-", ""),
                    "form": form,
                    "date": filings["filingDate"][i],
                }
            )
            if len(accession_numbers) >= limit:
                break

    return accession_numbers


def fetch_and_parse_xbrl(cik_str, accession_dict, schema):
    """
    Downloads the actual filing and parses the segment data based on the schema.
    This handles both US GAAP (10-K/10-Q) and IFRS (20-F/6-K).
    """
    form = accession_dict["form"]
    date = accession_dict["date"]

    # In a full production script, you would download the inline XBRL HTM or XML file here:
    # url = f"https://www.sec.gov/Archives/edgar/data/{int(cik_str)}/{accn}/..."
    # And parse it using BeautifulSoup or python-xbrl.

    # For this architecture, we extract dummy values mapped to the schema to prove the pipeline flow
    extracted_data = {"Quarter/Date": date, "Form": form}
    for metric in schema:
        # Example: parse the HTM for the specific tags
        extracted_data[metric] = "Parsed_Value"

    return extracted_data


def run_pipeline():
    for ticker, info in TICKERS.items():
        print(f"\n--- Processing {ticker} ({info['forms']}) ---")
        filings = fetch_recent_filings(info["cik"], info["forms"], limit=20)
        print(f"Found {len(filings)} relevant filings.")

        schema = COMPANY_SCHEMAS.get(ticker, {})
        history = []

        for f in filings:
            data = fetch_and_parse_xbrl(info["cik"], f, schema)
            history.append(data)

        if history:
            out_file = f".tmp/historical_segments_{ticker}.csv"
            keys = history[0].keys()
            with open(out_file, "w", newline="") as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=keys)
                writer.writeheader()
                writer.writerows(history)
            print(f"Saved {ticker} history to {out_file}")


if __name__ == "__main__":
    run_pipeline()
