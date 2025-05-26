import os
import sqlite3
import time
import logging
from datetime import datetime, timedelta, timezone
from telebot import TeleBot, types
from solana.rpc.api import Client
from solana.rpc.core import RPCException
import threading
from dotenv import load_dotenv
from flask import Flask, request, jsonify

# --- Load environment variables ---
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL")
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL") # This is for the warning only, actual set_webhook uses RAILWAY_PUBLIC_DOMAIN

# --- Validate Required Environment Variables ---
missing = []
if not TELEGRAM_BOT_TOKEN:
    missing.append("TELEGRAM_BOT_TOKEN")
if not SOLANA_RPC_URL:
    missing.append("SOLANA_RPC_URL")
# Removed WEBHOOK_BASE_URL from critical missing as it's not used by set_webhook's base_url directly.
# The warning still appears if WEBHOOK_BASE_URL is not set, as per previous logs.

if missing:
    logging.critical(f"❌ Critical: Missing environment variables: {', '.join(missing)}. Exiting.")
    exit(1)

DB_FILE = "rewards.db"
LOG_FILE = "reward_bot.log"

# --- Logging Setup ---
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

# --- Global Instances (wrapped in try-except for debugging) ---
bot = None
solana_client = None
conn = None
cursor = None

try:
    logging.info("Attempting to initialize Telegram Bot and Solana Client...")
    bot = TeleBot(TELEGRAM_BOT_TOKEN)
    solana_client = Client(SOLANA_RPC_URL)
    logging.info("Telegram Bot and Solana Client initialized.")
except ValueError as e:
    logging.critical(f"❌ CRITICAL ERROR: Token Validation Failed: {e}")
    # Re-raise to ensure process exits if token is invalid
    raise
except Exception as e:
    logging.critical(f"❌ CRITICAL ERROR during bot/solana client initialization: {e}")
    # Re-raise to ensure process exits on other critical errors
    raise

# --- Database Setup (wrapped in try-except for debugging) ---
db_lock = threading.Lock()
try:
    logging.info(f"Attempting to connect to database: {DB_FILE}")
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    cursor = conn.cursor()

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
    logging.info("Database connected and schema verified.")
except sqlite3.Error as e:
    logging.critical(f"❌ CRITICAL ERROR during database setup: {e}")
    raise
except Exception as e:
    logging.critical(f"❌ CRITICAL ERROR during database setup (general): {e}")
    raise

# --- Reward Constants ---
BUY_STREAK_REWARDS = {3: 50000, 5: 100000, 7: 200000, 14: 500000, 30: 1000000}
LEADERBOARD_REWARDS = {1: 2000000, 2: 1000000, 3: 500000}
REFERRAL_BONUS_CAP = 500000
LEADERBOARD_RESET_INTERVAL_HOURS = 24 * 7

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
        response = solana_client.get_signatures_for_address(wallet, limit=10)
        
        if response and 'result' in response:
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
@app.route("/", methods=["GET"])
def index():
    """Basic endpoint to confirm the bot is running."""
    logging.info("Received GET request to /") # Log access
    return "✅ MyNala Bot is running", 200

@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    logging.info("Received GET request to /health") # Log access
    return jsonify({"status": "ok"}), 200

@app.route(f"/webhook/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def webhook():
    """Telegram webhook endpoint to process incoming updates."""
    logging.info("Received POST request to webhook endpoint.") # Log webhook access
    if request.headers.get('content-type') == 'application/json':
        try:
            json_string = request.get_data().decode("utf-8")
            update = types.Update.de_json(json_string)
            bot.process_new_updates([update])
            logging.info("Successfully processed new Telegram update.") # Log successful processing
            return "OK", 200
        except Exception as e:
            logging.error(f"❌ Error processing Telegram update: {e}", exc_info=True) # Log errors with traceback
            return "Error", 500
    else:
        logging.warning(f"Received webhook request with invalid content type: {request.headers.get('content-type')}")
        return "Content-Type must be application/json", 400

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    logging.info(f"Received /start or /help command from chat_id {message.chat.id}")
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
    logging.info(f"Received /verify command from chat_id {message.chat.id}")
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
                        cursor.execute("UPDATE users SET verified=1, chat_id=? WHERE wallet=?", (chat_id, wallet))
                        conn.commit()
                        bot.reply_to(message, f"✅ Wallet {wallet[:6]}...{wallet[-4:]} is now verified and linked to your account.")
                else:
                    bot.reply_to(message, f"❌ Wallet {wallet[:6]}...{wallet[-4:]} is already linked to another Telegram account.")
            else:
                now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                cursor.execute("INSERT INTO users (chat_id, wallet, verified, last_purchase) VALUES (?, ?, ?, ?)",
                               (chat_id, wallet, 1, now))
                conn.commit()
                bot.reply_to(message, f"✅ Wallet {wallet[:6]}...{wallet[-4:]} is now verified and linked to your account.")
        except sqlite3.Error as e:
            logging.error(f"Database error during /verify for chat_id {chat_id}, wallet {wallet}: {e}", exc_info=True)
            bot.reply_to(message, "An error occurred while processing your request. Please try again.")
        except Exception as e:
            logging.error(f"General error during /verify for chat_id {chat_id}, wallet {wallet}: {e}", exc_info=True)
            bot.reply_to(message, "An unexpected error occurred. Please try again.")


@bot.message_handler(commands=['status'])
def check_status(message):
    logging.info(f"Received /status command from chat_id {message.chat.id}")
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /status <wallet_address>")
        return

    wallet = parts[1].strip()
    if not wallet:
        bot.reply_to(message, "Wallet address cannot be empty.")
        return

    with db_lock:
        try:
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
        except Exception as e:
            logging.error(f"Error during /status for chat_id {message.chat.id}, wallet {wallet}: {e}", exc_info=True)
            bot.reply_to(message, "An error occurred while fetching status. Please try again.")

@bot.message_handler(commands=['claim'])
def claim_rewards(message):
    logging.info(f"Received /claim command from chat_id {message.chat.id}")
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /claim <wallet_address>")
        return

    wallet = parts[1].strip()
    if not wallet:
        bot.reply_to(message, "Wallet address cannot be empty.")
        return

    with db_lock:
        try:
            cursor.execute("SELECT total_rewards FROM users WHERE wallet=?", (wallet,))
            result = cursor.fetchone()

            if result:
                bot.reply_to(message, f"💰 Wallet `{wallet[:6]}...{wallet[-4:]}` has {result[0]} $MN in rewards.")
            else:
                bot.reply_to(message, "Wallet not found.")
        except Exception as e:
            logging.error(f"Error during /claim for chat_id {message.chat.id}, wallet {wallet}: {e}", exc_info=True)
            bot.reply_to(message, "An error occurred while checking claims. Please try again.")

@bot.message_handler(commands=['referrals'])
def check_referrals(message):
    logging.info(f"Received /referrals command from chat_id {message.chat.id}")
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /referrals <wallet_address>")
        return

    wallet = parts[1].strip()
    if not wallet:
        bot.reply_to(message, "Wallet address cannot be empty.")
        return

    with db_lock:
        try:
            cursor.execute("SELECT referral_count FROM users WHERE wallet=?", (wallet,))
            result = cursor.fetchone()

            if result:
                bot.reply_to(message, f"📣 Wallet `{wallet[:6]}...{wallet[-4:]}` has {result[0]} referral(s).")
            else:
                bot.reply_to(message, "Wallet not found.")
        except Exception as e:
            logging.error(f"Error during /referrals for chat_id {message.chat.id}, wallet {wallet}: {e}", exc_info=True)
            bot.reply_to(message, "An error occurred while checking referrals. Please try again.")

@bot.message_handler(commands=['buy'])
def buy_tokens(message):
    logging.info(f"Received /buy command from chat_id {message.chat.id}")
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
        try:
            cursor.execute("SELECT streak_days, last_purchase, total_volume, total_rewards FROM users WHERE wallet=?", (wallet,))
            result = cursor.fetchone()

            if result:
                streak, last_purchase_str, volume, rewards = result

                if last_purchase_str:
                    last_purchase_date = datetime.strptime(last_purchase_str, "%Y-%m-%d").date()
                    if (now.date() - last_purchase_date).days == 1:
                        streak += 1
                    elif (now.date() - last_purchase_date).days == 0:
                        pass # Same day, don't break/increase streak
                    else:
                        streak = 1
                else:
                    streak = 1

                volume += amount
                reward = BUY_STREAK_REWARDS.get(streak, 0)
                rewards += reward

                cursor.execute("UPDATE users SET streak_days=?, last_purchase=?, total_volume=?, total_rewards=? WHERE wallet=?",
                                (streak, today_str, volume, rewards, wallet))
                conn.commit()
                bot.reply_to(message,
                                f"✅ Buy of {amount} $MN recorded for `{wallet[:6]}...{wallet[-4:]}`.\n"
                                f"🔥 Streak: {streak} days\n"
                                f"💰 Total Rewards: {rewards} $MN",
                                parse_mode='Markdown')
            else:
                bot.reply_to(message, "Wallet not found. Please verify first.")
        except Exception as e:
            logging.error(f"Error during /buy for chat_id {message.chat.id}, wallet {wallet}, amount {amount}: {e}", exc_info=True)
            bot.reply_to(message, "An error occurred while recording purchase. Please try again.")

# --- Webhook Management ---
def set_webhook():
    """Sets the Telegram bot webhook URL."""
    try:
        logging.info("Attempting to remove existing webhook...")
        bot.remove_webhook()
        time.sleep(1)

        base_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "https://primary-production-cd3d.up.railway.app")
        # Ensure base_url is explicitly HTTPS
        if base_url.startswith("http://"):
            base_url = base_url.replace("http://", "https://")
        elif not base_url.startswith("https://"):
            base_url = f"https://{base_url}"

        webhook_url = f"{base_url}/webhook/{TELEGRAM_BOT_TOKEN}"
        logging.info(f"Attempting to set webhook to {webhook_url}")
        
        result = bot.set_webhook(url=webhook_url)
        if result:
            logging.info(f"✅ Webhook successfully set to {webhook_url}")
        else:
            logging.error(f"❌ Failed to set webhook to {webhook_url}. Result: {result}")
    except Exception as e:
        logging.critical(f"❌ Exception in set_webhook: {e}", exc_info=True)

# --- Bot Startup ---
if __name__ == "__main__":
    logging.info("Bot startup initiated.")
    try:
        set_webhook()
        logging.info("Webhook setup attempted.")
    except Exception as e:
        logging.critical(f"❌ CRITICAL ERROR: Webhook setup failed during startup: {e}", exc_info=True)

    port = int(os.environ.get("PORT", 5000))
    # Flask app is managed by Gunicorn, so app.run() is not needed here
    # logging.info(f"Flask app will be run by Gunicorn on port {port}") # More accurate for Gunicorn
