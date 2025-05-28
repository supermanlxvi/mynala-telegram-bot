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
# WEBHOOK_BASE_URL is not strictly used for setting webhook URL, but for a warning
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL")

# --- Define DB_FILE and LOG_FILE at the top (CORRECTED PLACEMENT) ---
DB_FILE = "rewards.db"
LOG_FILE = "reward_bot.log"

# --- Validate Required Environment Variables ---
missing = []
if not TELEGRAM_BOT_TOKEN:
    missing.append("TELEGRAM_BOT_TOKEN")
if not SOLANA_RPC_URL:
    missing.append("SOLANA_RPC_URL")

if missing:
    logging.critical(f"❌ Critical: Missing environment variables: {', '.join(missing)}. Exiting.")
    exit(1)

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)

# --- Global Instances and App Initialization (Ensuring proper order for Gunicorn) ---
# These need to be initialized at the top level for Gunicorn to discover `app`
# and for all bot handlers to be registered properly before app starts serving.

# 1. Initialize Flask app
app = Flask(__name__)
logging.info("Flask app instance created.")

# 2. Initialize Telegram Bot and Solana Client
bot = None
solana_client = None
try:
    logging.info("Attempting to initialize Telegram Bot and Solana Client...")
    bot = TeleBot(TELEGRAM_BOT_TOKEN)
    solana_client = Client(SOLANA_RPC_URL)
    logging.info("Telegram Bot and Solana Client initialized.")
except ValueError as e:
    logging.critical(f"❌ CRITICAL ERROR: Token Validation Failed: {e}", exc_info=True)
    raise # Re-raise to ensure process exits if token is invalid
except Exception as e:
    logging.critical(f"❌ CRITICAL ERROR during bot/solana client initialization: {e}", exc_info=True)
    raise # Re-raise to ensure process exits on other critical errors

# 3. Initialize Database connection and cursor
conn = None
cursor = None
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
    logging.critical(f"❌ CRITICAL ERROR during database setup: {e}", exc_info=True)
    raise
except Exception as e:
    logging.critical(f"❌ CRITICAL ERROR during database setup (general): {e}", exc_info=True)
    raise

# --- Reward Constants ---
BUY_STREAK_REWARDS = {3: 50000, 5: 100000, 7: 200000, 14: 500000, 30: 1000000}
LEADERBOARD_REWARDS = {1: 2000000, 2: 1000000, 3: 500000}
REFERRAL_BONUS_CAP = 500000
LEADERBOARD_RESET_INTERVAL_HOURS = 24 * 7

# --- Solana Transaction Verification ---
def verify_transaction(wallet: str, amount: int) -> bool:
    try:
        response = solana_client.get_signatures_for_address(wallet, limit=10)
        
        if response and 'result' in response:
            if response['result']:
                logging.info(f"Recent transactions found for wallet {wallet}. Further verification (amount/memo) requires fetching full transaction details.")
                return True
        logging.info(f"No recent transactions found for wallet {wallet} or RPC response is empty.")
        return False
    except RPCException as e:
        logging.error(f"Solana RPC error during transaction check for {wallet}: {e}", exc_info=True)
        return False
    except Exception as e:
        logging.error(f"General error during transaction check for {wallet}: {e}", exc_info=True)
        return False

# --- Webhook Management Function (defined, but call moved for Gunicorn) ---
def set_webhook_on_startup():
    logging.info("Webhook setup initiated on startup thread.")
    # Add a small delay to ensure Flask app is fully listening
    time.sleep(5) # Give Flask app time to bind to port and Gunicorn to warm up

    try:
        logging.info("Attempting to remove existing webhook...")
        bot.remove_webhook()
        time.sleep(1)

        # Render uses `RENDER_EXTERNAL_HOSTNAME` for its public URL
        base_url = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
        if not base_url:
            logging.critical("❌ CRITICAL ERROR: RENDER_EXTERNAL_HOSTNAME environment variable is not set. Cannot set webhook.")
            return False

        # Ensure base_url is explicitly HTTPS
        if not base_url.startswith("http"): # Check for any http/https
            base_url = f"https://{base_url}" # Default to HTTPS if not present
        elif base_url.startswith("http://"): # If it's http, change to https
            base_url = base_url.replace("http://", "https://")


        webhook_url = f"{base_url}/webhook/{TELEGRAM_BOT_TOKEN}"
        logging.info(f"Attempting to set webhook to {webhook_url}")
        
        result = bot.set_webhook(url=webhook_url)
        if result:
            logging.info(f"✅ Webhook successfully set to {webhook_url}")
            return True
        else:
            logging.error(f"❌ Failed to set webhook to {webhook_url}. Result: {result}")
            return False
    except Exception as e:
        logging.critical(f"❌ Exception in set_webhook_on_startup: {e}", exc_info=True)
        return False

# --- Command Handlers (defined here, after bot is initialized) ---
@app.route("/", methods=["GET"])
def index():
    logging.info("Received GET request to /")
    return "✅ MyNala Bot is running", 200

@app.route("/health", methods=["GET"])
def health():
    logging.info("Received GET request to /health")
    return jsonify({"status": "ok"}), 200

@app.route(f"/webhook/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def webhook():
    logging.info("Received POST request to webhook endpoint.")
    if request.headers.get('content-type') == 'application/json':
        try:
            json_string = request.get_data().decode("utf-8")
            update = types.Update.de_json(json_string)

            # --- NEW LOGGING ADDED HERE ---
            logging.info(f"Processing update ID: {update.update_id}")
            if update.message:
                logging.info(f"  Update Type: Message")
                logging.info(f"  Chat ID: {update.message.chat.id}")
                logging.info(f"  Chat Type: {update.message.chat.type}")
                logging.info(f"  Message Text: '{update.message.text}'")
                logging.info(f"  Is Command: {update.message.text.startswith('/')}") # Check if it looks like a command
            elif update.channel_post:
                logging.info(f"  Update Type: Channel Post")
                logging.info(f"  Chat ID: {update.channel_post.chat.id}")
                logging.info(f"  Chat Type: {update.channel_post.chat.type}")
                logging.info(f"  Message Text: '{update.channel_post.text}'")
                logging.info(f"  Is Command: {update.channel_post.text.startswith('/')}") # Check if it looks like a command
            else:
                logging.info(f"  Update Type: Other (Not Message or Channel Post). Keys: {update.to_dict().keys()}")
            # --- END NEW LOGGING ---

            bot.process_new_updates([update])
            logging.info("Successfully processed new Telegram update.")
            return "OK", 200
        except Exception as e:
            logging.error(f"❌ Error processing Telegram update: {e}", exc_info=True)
            return "Error", 500
    else:
        logging.warning(f"Received webhook request with invalid content type: {request.headers.get('content-type')}")
        return "Content-Type must be application/json", 400

# Important: message handlers must be defined AFTER `bot = TeleBot(TELEGRAM_BOT_TOKEN)`
# and before `set_webhook()` is called (if called automatically).
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
    logging.info(f"Received /buy command from chat_id (message.chat_id}")
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

# --- Manual Webhook Setup (Moved to ensure Gunicorn starts serving first) ---
# This function will be called AFTER the Flask app is fully set up.
def set_webhook_on_startup():
    logging.info("Webhook setup initiated on startup thread.")
    # Add a small delay to ensure Flask app is fully listening
    time.sleep(5) # Give Flask app time to bind to port and Gunicorn to warm up

    try:
        logging.info("Attempting to remove existing webhook...")
        bot.remove_webhook()
        time.sleep(1)

        # Render uses `RENDER_EXTERNAL_HOSTNAME` for its public URL
        base_url = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
        if not base_url:
            logging.critical("❌ CRITICAL ERROR: RENDER_EXTERNAL_HOSTNAME environment variable is not set. Cannot set webhook.")
            return False

        # Ensure base_url is explicitly HTTPS
        if not base_url.startswith("http"): # Check for any http/https
            base_url = f"https://{base_url}" # Default to HTTPS if not present
        elif base_url.startswith("http://"): # If it's http, change to https
            base_url = base_url.replace("http://", "https://")


        webhook_url = f"{base_url}/webhook/{TELEGRAM_BOT_TOKEN}"
        logging.info(f"Attempting to set webhook to {webhook_url}")
        
        result = bot.set_webhook(url=webhook_url)
        if result:
            logging.info(f"✅ Webhook successfully set to {webhook_url}")
            return True
        else:
            logging.error(f"❌ Failed to set webhook to {webhook_url}. Result: {result}")
            return False
    except Exception as e:
        logging.critical(f"❌ Exception in set_webhook_on_startup: {e}", exc_info=True)
        return False

# ... (rest of your code above webhook function)

# Call set_webhook_on_startup in a separate thread AFTER all routes are defined and app is ready
# This allows Gunicorn to fully start the Flask app and bind to its port before the webhook call.
try:
    logging.info("Spawning thread for webhook setup...")
    webhook_thread = threading.Thread(target=set_webhook_on_startup)
    webhook_thread.start()
    logging.info("Webhook setup thread started.")
except Exception as e:
    logging.critical(f"❌ CRITICAL ERROR: Could not spawn webhook setup thread: {e}", exc_info=True)