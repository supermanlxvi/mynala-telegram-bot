import os
import sqlite3
import time
import logging
from datetime import datetime, timedelta, timezone
from telebot import TeleBot, types
from solana.rpc.api import Client
from solana.rpc.core import RPCException # Import for better error handling
import threading
from dotenv import load_dotenv
from flask import Flask, request, jsonify

# --- Load environment variables ---
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL")
# Use a more generic environment variable for the webhook base URL
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL")

# --- Validate Required Environment Variables ---
missing = []
if not TELEGRAM_BOT_TOKEN:
    missing.append("TELEGRAM_BOT_TOKEN")
if not SOLANA_RPC_URL:
    missing.append("SOLANA_RPC_URL")
if not WEBHOOK_BASE_URL:
    logging.warning("⚠️ WEBHOOK_BASE_URL not set. Webhook might not function correctly without a public URL.")
    # Provide a placeholder or default if not set, but warn the user.
    # For local testing, this might be fine, but for deployment, it's critical.
    WEBHOOK_BASE_URL = "http://localhost:5000" # Placeholder for local development

if missing:
    logging.critical(f"❌ Critical: Missing environment variables: {', '.join(missing)}. Exiting.")
    exit(1)

DB_FILE = "rewards.db"
LOG_FILE = "reward_bot.log" # This is defined but not used in basicConfig, which logs to console by default.

# --- Logging Setup ---
# Configure logging to also write to a file
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)

# --- Flask App for Webhook ---
app = Flask(__name__)

@app.route("/", methods=["GET"])
def index():
    """Basic endpoint to confirm the bot is running."""
    return "✅ MyNala Bot is running", 200

@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok"}), 200

@app.route(f"/webhook/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def webhook():
    """Telegram webhook endpoint to process incoming updates."""
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode("utf-8")
        update = types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "OK", 200
    else:
        logging.warning(f"Received webhook request with invalid content type: {request.headers.get('content-type')}")
        return "Content-Type must be application/json", 400

# --- Global Instances ---
bot = TeleBot(TELEGRAM_BOT_TOKEN)
solana_client = Client(SOLANA_RPC_URL)

# --- Database Setup ---
db_lock = threading.Lock()
# Using a context manager for database connection for better resource management
# The connection will be opened and closed per operation or managed by the lock.
# For a Flask app with threading, it's safer to open connection per request or use a pool.
# For simplicity, keeping the global connection but emphasizing lock usage.
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cursor = conn.cursor()

# Create table if not exists
cursor.execute('''
CREATE TABLE IF NOT EXISTS users (
    chat_id INTEGER PRIMARY KEY,
    verified INTEGER DEFAULT 0,
    wallet TEXT UNIQUE,
    streak_days INTEGER DEFAULT 0,
    last_purchase TEXT DEFAULT NULL,
    total_volume INTEGER DEFAULT 0,
    referral_count INTEGER DEFAULT 0,
    total_rewards INTEGER DEFAULT 0,
    referred_by TEXT DEFAULT NULL
)
''')
conn.commit()

# --- Reward Constants ---
BUY_STREAK_REWARDS = {3: 50000, 5: 100000, 7: 200000, 14: 500000, 30: 1000000}
LEADERBOARD_REWARDS = {1: 2000000, 2: 1000000, 3: 500000}
REFERRAL_BONUS_CAP = 500000
LEADERBOARD_RESET_INTERVAL_HOURS = 24 * 7 # Defined but not implemented in current logic

# --- Solana Transaction Verification ---
def verify_transaction(wallet: str, min_amount: int) -> bool:
    """
    Verifies if a transaction of at least min_amount has occurred for the given wallet.
    NOTE: This function checks for *any* transaction for the wallet.
    To verify specific amounts or memo fields, you would need to fetch
    full transaction details using solana_client.get_transaction(signature).
    This would involve more RPC calls and complexity.
    """
    try:
        # Using get_signatures_for_address for recent signatures
        # This only returns transaction signatures, not full transaction details.
        # To verify memo or exact amount, you would need to iterate through these
        # signatures and call solana_client.get_transaction(signature) for each.
        response = solana_client.get_signatures_for_address(wallet, limit=10) # Limit to recent 10 for performance
        
        if response and 'result' in response:
            # For the purpose of this simplified verification, we'll just check if any transactions exist.
            # A real implementation would parse transaction details for amount/memo.
            if response['result']:
                logging.info(f"Recent transactions found for wallet {wallet}. Further verification (amount/memo) requires fetching full transaction details.")
                return True
        logging.info(f"No recent transactions found for wallet {wallet} or RPC response is empty.")
        return False
    except RPCException as e:
        logging.error(f"Solana RPC error during transaction check for {wallet}: {e}")
        return False
    except Exception as e:
        logging.error(f"General error during transaction check for {wallet}: {e}")
        return False

# --- Command Handlers ---
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    """Handles the /start and /help commands, providing bot information."""
    bot.reply_to(message,
                 "Welcome to MyNala Rewards Bot! 🚀\n\n"
                 "Available commands:\n"
                 "/verify <wallet> - Verify your wallet and link it to your Telegram chat ID.\n"
                 "/status <wallet> - Check your current rewards status, streak, and volume.\n"
                 "/buy <wallet> <amount> - Record a purchase and update your streak/volume.\n"
                 "/claim <wallet> - View your total claimable rewards.\n"
                 "/referrals <wallet> - Check your referral count.")

@bot.message_handler(commands=['verify'])
def verify_wallet(message):
    """Handles the /verify command to link a wallet to the user's chat ID."""
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /verify <wallet_address>")
        return

    wallet = parts[1].strip()
    chat_id = message.chat.id

    if not wallet:
        bot.reply_to(message, "Wallet address cannot be empty.")
        return

    with db_lock:
        try:
            cursor.execute("SELECT chat_id, verified FROM users WHERE wallet=?", (wallet,))
            result = cursor.fetchone()

            if result:
                existing_chat_id, current_verified_status = result
                if existing_chat_id == chat_id:
                    if current_verified_status:
                        bot.reply_to(message, f"✅ Wallet {wallet[:6]}...{wallet[-4:]} is already verified and linked to your account.")
                    else:
                        # If wallet exists but not verified, update it
                        cursor.execute("UPDATE users SET verified=1, chat_id=? WHERE wallet=?", (chat_id, wallet))
                        conn.commit()
                        bot.reply_to(message, f"✅ Wallet {wallet[:6]}...{wallet[-4:]} is now verified and linked to your account.")
                else:
                    # Wallet exists but linked to a different chat_id
                    bot.reply_to(message, f"❌ Wallet {wallet[:6]}...{wallet[-4:]} is already linked to another Telegram account.")
            else:
                # New wallet, insert it
                now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                cursor.execute("INSERT INTO users (chat_id, wallet, verified, last_purchase) VALUES (?, ?, ?, ?)",
                               (chat_id, wallet, 1, now))
                conn.commit()
                bot.reply_to(message, f"✅ Wallet {wallet[:6]}...{wallet[-4:]} is now verified and linked to your account.")
        except sqlite3.Error as e:
            logging.error(f"Database error during /verify for chat_id {chat_id}, wallet {wallet}: {e}")
            bot.reply_to(message, "An error occurred while processing your request. Please try again.")

@bot.message_handler(commands=['status'])
def check_status(message):
    """Handles the /status command to display user's reward status."""
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /status <wallet_address>")
        return

    wallet = parts[1].strip()
    if not wallet:
        bot.reply_to(message, "Wallet address cannot be empty.")
        return

    with db_lock:
        cursor.execute("SELECT verified, streak_days, total_volume, total_rewards FROM users WHERE wallet=?", (wallet,))
        result = cursor.fetchone()

    if result:
        verified, streak, volume, rewards = result
        status = "✅ Verified" if verified else "❌ Not Verified"
        bot.reply_to(message,
                     f"📊 Status for `{wallet[:6]}...{wallet[-4:]}`:\n"
                     f"{status}\n"
                     f"📈 Volume: {volume} $MN\n"
                     f"🔥 Streak: {streak} days\n"
                     f"💰 Rewards: {rewards} $MN",
                     parse_mode='Markdown')
    else:
        bot.reply_to(message, "Wallet not found. Please verify it first using /verify <wallet_address>.")

@bot.message_handler(commands=['claim'])
def claim_rewards(message):
    """Handles the /claim command to show total claimable rewards."""
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /claim <wallet_address>")
        return

    wallet = parts[1].strip()
    if not wallet:
        bot.reply_to(message, "Wallet address cannot be empty.")
        return

    with db_lock:
        cursor.execute("SELECT total_rewards FROM users WHERE wallet=?", (wallet,))
        result = cursor.fetchone()

    if result:
        bot.reply_to(message, f"💰 Wallet `{wallet[:6]}...{wallet[-4:]}` has {result[0]} $MN in rewards.")
    else:
        bot.reply_to(message, "Wallet not found. Please verify it first.")

@bot.message_handler(commands=['referrals'])
def check_referrals(message):
    """Handles the /referrals command to show referral count."""
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /referrals <wallet_address>")
        return

    wallet = parts[1].strip()
    if not wallet:
        bot.reply_to(message, "Wallet address cannot be empty.")
        return

    with db_lock:
        cursor.execute("SELECT referral_count FROM users WHERE wallet=?", (wallet,))
        result = cursor.fetchone()

    if result:
        bot.reply_to(message, f"📣 Wallet `{wallet[:6]}...{wallet[-4:]}` has {result[0]} referral(s).")
    else:
        bot.reply_to(message, "Wallet not found. Please verify it first.")

@bot.message_handler(commands=['buy'])
def buy_tokens(message):
    """Handles the /buy command to record a purchase and update user stats."""
    parts = message.text.split()
    if len(parts) < 3:
        bot.reply_to(message, "Usage: /buy <wallet_address> <amount>")
        return

    wallet = parts[1].strip()
    if not wallet:
        bot.reply_to(message, "Wallet address cannot be empty.")
        return

    try:
        amount = int(parts[2])
        if amount <= 0:
            bot.reply_to(message, "Amount must be a positive integer.")
            return
    except ValueError:
        bot.reply_to(message, "Amount must be an integer.")
        return

    chat_id = message.chat.id
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")

    with db_lock:
        cursor.execute("SELECT streak_days, last_purchase, total_volume, total_rewards FROM users WHERE wallet=?", (wallet,))
        result = cursor.fetchone()

        if result:
            streak, last_purchase_str, volume, rewards = result

            # Handle last_purchase being NULL
            if last_purchase_str:
                last_purchase_date = datetime.strptime(last_purchase_str, "%Y-%m-%d").date()
                # Check if today is the day after the last purchase for streak
                if (now.date() - last_purchase_date).days == 1:
                    streak += 1
                elif (now.date() - last_purchase_date).days == 0:
                    # If purchase is on the same day, don't break streak, don't increase it either.
                    # This prevents multiple streak increments for same-day purchases.
                    pass
                else:
                    # Streak broken or first purchase in a while
                    streak = 1
            else:
                # First purchase recorded for this user
                streak = 1

            volume += amount
            reward = BUY_STREAK_REWARDS.get(streak, 0)
            rewards += reward

            try:
                cursor.execute("UPDATE users SET streak_days=?, last_purchase=?, total_volume=?, total_rewards=? WHERE wallet=?",
                               (streak, today_str, volume, rewards, wallet))
                conn.commit()
                bot.reply_to(message,
                             f"✅ Buy of {amount} $MN recorded for `{wallet[:6]}...{wallet[-4:]}`.\n"
                             f"🔥 Streak: {streak} days\n"
                             f"💰 Total Rewards: {rewards} $MN",
                             parse_mode='Markdown')
            except sqlite3.Error as e:
                logging.error(f"Database error during /buy for wallet {wallet}, amount {amount}: {e}")
                bot.reply_to(message, "An error occurred while recording your purchase. Please try again.")
        else:
            bot.reply_to(message, "Wallet not found. Please verify it first using /verify <wallet_address>.")

# --- Webhook Management ---
def set_webhook():
    """Sets the Telegram bot webhook URL."""
    try:
        logging.info("Attempting to remove existing webhook...")
        bot.remove_webhook()
        time.sleep(1) # Give a moment for the webhook to be removed

        # Use the WEBHOOK_BASE_URL environment variable
        webhook_url = f"{WEBHOOK_BASE_URL}/webhook/{TELEGRAM_BOT_TOKEN}"
        logging.info(f"Attempting to set webhook to: {webhook_url}")
        result = bot.set_webhook(url=webhook_url)

        if result:
            logging.info(f"✅ Webhook successfully set to {webhook_url}")
        else:
            logging.error(f"❌ Failed to set webhook to {webhook_url}. Result: {result}")
    except Exception as e:
        logging.critical(f"❌ Exception in set_webhook: {e}")
        # Consider exiting or retrying if webhook setup is critical for operation.

# --- Bot Startup ---
if __name__ == "__main__":
    # Set the webhook when the application starts
    set_webhook()
    
    # Get port from environment, default to 5000 for local development
    port = int(os.environ.get("PORT", 5000))
    logging.info(f"Starting Flask app on port {port}")
    # In a production environment, debug=True should be avoided.
    # Use a production-ready WSGI server like Gunicorn or uWSGI.
    app.run(host="0.0.0.0", port=port, debug=True)