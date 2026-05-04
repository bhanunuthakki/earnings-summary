import os
import sys

# Make sure we can import from src
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(project_root, 'src'))

import requests
import pandas as pd
from io import StringIO
from calendar_manager import get_earnings_date

def test_sp500():
    print("\n--- Testing S&P 500 Fetch ---")
    url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    try:
        resp = requests.get(url, headers=headers)
        if resp.status_code == 200:
            tables = pd.read_html(StringIO(resp.text))
            df = tables[0]
            if 'Symbol' in df.columns and 'Security' in df.columns:
                tickers = df['Symbol'].tolist()
                print(f"✅ S&P 500 fetch successful. Retrieved {len(tickers)} companies.")
                print(f"Sample: {tickers[0]} - {df['Security'][0]}")
            else:
                print("❌ S&P 500 fetch failed: Missing 'Symbol' or 'Security' column.")
        else:
            print(f"❌ S&P 500 fetch failed: HTTP {resp.status_code}")
    except Exception as e:
        print(f"❌ S&P 500 fetch failed with exception: {e}")

def test_russell2000():
    print("\n--- Testing Russell 2000 Fetch ---")
    url = "https://raw.githubusercontent.com/ikoniaris/Russell2000/master/russell_2000_components.csv"
    try:
        df = pd.read_csv(url)
        if 'Ticker' in df.columns and 'Name' in df.columns:
            tickers = df['Ticker'].tolist()
            print(f"✅ Russell 2000 fetch successful. Retrieved {len(tickers)} companies.")
            print(f"Sample: {tickers[0]} - {df['Name'][0]}")
        else:
            print("❌ Russell 2000 fetch failed: Missing 'Ticker' or 'Name' column.")
    except Exception as e:
        print(f"❌ Russell 2000 fetch failed with exception: {e}")

def test_earnings_date():
    print("\n--- Testing Earnings Date Fetch (AAPL) ---")
    try:
        date = get_earnings_date('AAPL')
        if date:
            print(f"✅ Earnings date fetch successful for AAPL: {date}")
        else:
            print("❌ Earnings date fetch failed for AAPL (returned None, maybe API key issue or no date set).")
    except Exception as e:
        print(f"❌ Earnings date fetch failed with exception: {e}")

def test_fmp_api():
    print("\n--- Testing FMP API (Available Quarters) ---")
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(project_root, '.env'))
        api_key = os.environ.get("FMP_API_KEY")
        if not api_key:
            print("❌ FMP_API_KEY not found in .env. Skipping test.")
            return

        url = f"https://financialmodelingprep.com/api/v4/earning_call_transcript?symbol=AAPL&apikey={api_key}"
        res = requests.get(url)
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, list) and len(data) > 0:
                print(f"✅ FMP API fetch successful. Retrieved {len(data)} historical quarters for AAPL.")
                print(f"Sample: {data[0]}")
            else:
                print("❌ FMP API fetch failed: Returned empty or invalid format.")
        else:
             print(f"❌ FMP API fetch failed: HTTP {res.status_code}")
    except Exception as e:
        print(f"❌ FMP API fetch failed with exception: {e}")

if __name__ == "__main__":
    print("Starting Data Fetch Validation Tests...\n")
    test_sp500()
    test_russell2000()
    test_earnings_date()
    test_fmp_api()
    print("\nTests completed.")
