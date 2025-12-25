import os
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

load_dotenv()

def find_market(keyword):
    host = "https://clob.polymarket.com"
    key = os.getenv("PRIVATE_KEY")
    creds = ApiCreds(
        api_key=os.getenv("CLOB_API_KEY"),
        api_secret=os.getenv("CLOB_API_SECRET"),
        api_passphrase=os.getenv("CLOB_API_PASSPHRASE")
    )
    client = ClobClient(host, key=key, chain_id=137, creds=creds)
    
    cursor = ""
    while True:
        resp = client.get_markets(next_cursor=cursor)
        markets = resp.get('data', [])
        for m in markets:
            if keyword.lower() in m.get('question', '').lower() or \
               keyword.lower() in m.get('market_slug', '').lower():
                print(f"Found Market: {m.get('question')}")
                print(f"Slug: {m.get('market_slug')}")
                print(f"Condition ID: {m.get('condition_id')}")
                print(f"Tokens: {m.get('tokens')}")
                # return m
        
        cursor = resp.get('next_cursor')
        if not cursor:
            break
    print("Search complete.")

if __name__ == "__main__":
    import sys
    kw = sys.argv[1] if len(sys.argv) > 1 else "btc"
    find_market(kw)
