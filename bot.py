import os
import time
import logging
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, ApiCreds, BalanceAllowanceParams, OpenOrderParams, AssetType
from py_clob_client.constants import POLYGON

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

class PolymarketBot:
    def __init__(self):
        self.host = "https://clob.polymarket.com"
        self.key = os.getenv("CLOB_API_KEY")
        self.secret = os.getenv("CLOB_API_SECRET")
        self.passphrase = os.getenv("CLOB_API_PASSPHRASE")
        self.private_key = os.getenv("PRIVATE_KEY")
        self.chain_id = int(os.getenv("CHAIN_ID", 137))
        
        creds = ApiCreds(
            api_key=self.key,
            api_secret=self.secret,
            api_passphrase=self.passphrase
        )
        
        self.client = ClobClient(
            self.host,
            chain_id=self.chain_id,
            key=self.private_key,
            creds=creds
        )
        
        # Explicitly set API credentials for authenticated requests
        self.client.set_api_creds(creds)
        
    def resolve_slug_to_condition_id(self, market_slug):
        """Use Gamma API to resolve slug to condition_id."""
        try:
            url = f"https://gamma-api.polymarket.com/markets?slug={market_slug}"
            resp = requests.get(url)
            if resp.status_code == 200:
                data = resp.json()
                if data and len(data) > 0:
                    return data[0].get('conditionId')
            return None
        except Exception as e:
            logger.error(f"Error resolving slug via Gamma API: {e}")
            return None

    def get_market_details(self, market_slug):
        """Fetch market details by slug, resolving via Gamma if necessary."""
        try:
            # 1. Try resolving via Gamma API first to get condition_id and granular data
            url = f"https://gamma-api.polymarket.com/markets?slug={market_slug}"
            resp = requests.get(url)
            gamma_data = None
            if resp.status_code == 200:
                data = resp.json()
                if data and len(data) > 0:
                    gamma_data = data[0]
                    condition_id = gamma_data.get('conditionId')
                    logger.info(f"Resolved slug {market_slug} to Condition ID: {condition_id}")
                    market = self.client.get_market(condition_id)
                    # Merge granular data from Gamma (like exact endDate)
                    market.update(gamma_data)
                    return market
            
            # 2. Fallback to direct slug lookup
            market = self.client.get_market(market_slug)
            return market
        except Exception as e:
            logger.error(f"Error fetching market details: {e}")
            return None

    def check_time_remaining(self, market_data, threshold_minutes=13):
        """Check if remaining time is more than threshold."""
        # Use Gamma API granular timestamp if available, fallback to CLOB
        close_time_str = market_data.get('endDate') or market_data.get('end_date_iso')
        if not close_time_str:
            logger.warning("Could not find close time in market data.")
            return False
            
        # Standardize format for fromisoformat
        close_time = datetime.fromisoformat(close_time_str.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        remaining = (close_time - now).total_seconds() / 60
        
        logger.info(f"Market close time: {close_time}")
        logger.info(f"Time remaining: {remaining:.2f} minutes")
        return remaining > threshold_minutes

    def place_ladder_orders(self, token_id, prices, size):
        """Place laddered limit orders."""
        order_ids = []
        for price in prices:
            try:
                order_args = OrderArgs(
                    price=price,
                    size=size,
                    side="BUY",
                    token_id=token_id
                )
                resp = self.client.create_and_post_order(order_args)
                if resp and isinstance(resp, dict) and resp.get('success'):
                    order_id = resp.get('orderID')
                    order_ids.append(order_id)
                    logger.info(f"Placed LIMIT order at {price}: {order_id}")
                else:
                    logger.error(f"Failed to place order at {price}: {resp}")
            except Exception as e:
                logger.error(f"Exception placing order: {e}")
        return order_ids

    def get_position_value(self, token_id, amount):
        """Calculate current value of positions based on order book mid-price."""
        try:
            ob = self.client.get_order_book(token_id)
            # Simple mid-price calculation
            bids = ob.get('bids', [])
            asks = ob.get('asks', [])
            if not bids or not asks:
                return 0
            
            best_bid = float(bids[0]['price'])
            best_ask = float(asks[0]['price'])
            mid_price = (best_bid + best_ask) / 2
            return amount * mid_price
        except Exception as e:
            logger.error(f"Error getting position value: {e}")
            return 0

    def close_all_positions(self, token_id, amount):
        """Cancel orders and sell all held tokens."""
        # 1. Cancel open orders
        try:
            params = OpenOrderParams(asset_id=token_id)
            open_orders = self.client.get_orders(params)
            for order in open_orders:
                order_id = order.get('orderID')
                logger.info(f"Cancelling order {order_id}...")
                self.client.cancel(order_id)
        except Exception as e:
            logger.error(f"Error cancelling orders: {e}")
            
        # 2. Sell tokens (Market Sell)
        try:
            if amount > 0:
                logger.info(f"Selling {amount} shares of {token_id}...")
                # We use a very low price to ensure it acts like a market sell
                # but Polymarket CLOB requires limit orders.
                order_args = OrderArgs(
                    price=0.01, 
                    size=amount,
                    side="SELL",
                    token_id=token_id
                )
                self.client.create_and_post_order(order_args)
                logger.info("Sell order placed.")
        except Exception as e:
            logger.error(f"Error selling positions: {e}")

    def get_token_balance(self, token_id):
        """Get the current balance of a specific token."""
        try:
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id
            )
            resp = self.client.get_balance_allowance(params)
            return float(resp.get('balance', 0))
        except Exception as e:
            logger.error(f"Error getting token balance: {e}")
            return 0

    def monitor_and_close(self, token_id, initial_total_cost):
        """Monitor positions and close when 30% profit reached."""
        logger.info(f"Monitoring Token ID: {token_id}")
        logger.info(f"Target Profit: {initial_total_cost * 1.3:.4f} USDC (Total Value)")
        
        while True:
            try:
                balance = self.get_token_balance(token_id)
                if balance <= 0:
                    # check if we still have open orders
                    params = OpenOrderParams(asset_id=token_id)
                    open_orders = self.client.get_orders(params)
                    if not open_orders:
                        logger.info("No balance and no open orders. Exiting monitoring.")
                        break
                    logger.info("Waiting for orders to fill...")
                    time.sleep(10)
                    continue

                # Get current price
                ob = self.client.get_order_book(token_id)
                asks = ob.get('asks', [])
                if not asks:
                    logger.warning("No asks in orderbook, cannot calculate sell value.")
                    time.sleep(10)
                    continue

                # Use best bid (since we want to sell)
                bids = ob.get('bids', [])
                if not bids:
                    logger.warning("No bids in orderbook, cannot calculate sell value.")
                    time.sleep(10)
                    continue
                
                current_price = float(bids[0]['price'])
                current_value = balance * current_price
                
                # Check for 30% profit
                # initial_total_cost is the total USDC spent to acquire 'balance'
                # For simplicity, if we don't track exact fills yet, we use a fixed target
                if current_value >= initial_total_cost * 1.3:
                    logger.info(f"PROFIT TARGET REACHED: {current_value:.4f} >= {initial_total_cost * 1.3:.4f}")
                    self.close_all_positions(token_id, balance)
                    break
                else:
                    logger.info(f"Current Value: {current_value:.4f} | Target: {initial_total_cost * 1.3:.4f}")
                
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                
            time.sleep(15)

    def run(self, market_slug, time_threshold=13, outcome_index=1):
        """Main execution loop."""
        market_data = self.get_market_details(market_slug)
        if not market_data:
            logger.error("Could not find market data.")
            return

        # outcome_index: 0 for YES/UP, 1 for NO/DOWN
        tokens = market_data.get('tokens', [])
        if not tokens:
            # Try to get clobTokenIds from Gamma data if tokens list is empty
            clob_token_ids = market_data.get('clobTokenIds')
            if clob_token_ids and isinstance(clob_token_ids, str):
                import json
                clob_token_ids = json.loads(clob_token_ids)
                token_id = clob_token_ids[outcome_index]
            else:
                logger.error("No tokens found in market data.")
                return
        else:
            token_id = tokens[outcome_index].get('token_id')
            
        logger.info(f"Using Token ID: {token_id} for outcome DOWN")

        # Step 1: Check time
        if not self.check_time_remaining(market_data, time_threshold):
            logger.info(f"Less than {time_threshold} minutes remaining. Skipping trade.")
            return

        # Step 2: Place ladder orders
        prices = [0.10, 0.20, 0.30]
        size_per_step = 10
        total_max_cost = sum(prices) * size_per_step
        
        logger.info(f"Placing ladder orders. Max theoretical cost: {total_max_cost} USDC")
        self.place_ladder_orders(token_id, prices, size_per_step)
        
        # Step 3: Monitor
        self.monitor_and_close(token_id, total_max_cost)

def parse_url(url):
    """Extract slug from Polymarket URL."""
    try:
        # Example: https://polymarket.com/event/btc-updown-15m-1766699100?tid=1766699194314
        path = url.split("?")[0]
        slug = path.split("/")[-1]
        return slug
    except Exception:
        return None

if __name__ == "__main__":
    bot = PolymarketBot()
    
    print("\n--- Polymarket Ladder Bot ---")
    market_url = input("Please enter the Market URL: ").strip()
    threshold = input("Enter time threshold in minutes (default 13): ").strip()
    
    if not threshold:
        threshold = 13
    else:
        threshold = int(threshold)
        
    slug = parse_url(market_url)
    if not slug:
        print("Invalid URL format.")
    else:
        bot.run(slug, time_threshold=threshold)
