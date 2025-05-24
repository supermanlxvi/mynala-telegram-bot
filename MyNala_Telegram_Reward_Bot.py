import os
import sqlite3
import time
import logging
import sys
from datetime import datetime, timedelta, timezone
from telebot import TeleBot, types
from solana.rpc.api import Client
import threading
from dotenv import load_dotenv
from flask import Flask, request, jsonify

# --- Load environment variables from .env FIRST ---
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL")
DB_FILE = "rewards.db"
LOG_FILE = "reward_bot.log"

# --- Logging Setup ---
logging.basicConfig(
    filename=LOG_FILE, 
    level=logging.INFO, 
    format="%(asctime)s - %(levelname)s - %(message)s"
)
# Add console handler for better debugging
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
console_handler.setFormatter(formatter)
logging.getLogger().addHandler(console_handler)

# --- Flask App for Webhook ---
app = Flask(__name__)

@app.route("/", methods=["GET"])
def index():
    return "✅ MyNala Bot is running", 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

@app.route(f"/webhook/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        logging.info("✅ Received Telegram webhook POST")
        json_string = request.get_data().decode("utf-8")
        update = types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "OK", 200
    except Exception as e:
        logging.error(f"Error processing webhook: {e}")
        return str(e), 500

# --- Global Instances ---
bot = TeleBot(TELEGRAM_BOT_TOKEN)
solana_client = Client(SOLANA_RPC_URL)

# --- Database Setup ---
db_lock = threading.Lock()

def get_db_connection():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    try:
        conn = get_db_connection()
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
        conn.close()
        logging.info("Database initialized successfully")
    except Exception as e:
        logging.error(f"Database initialization error: {e}")

init_db()

# --- Reward Constants ---
BUY_STREAK_REWARDS = {3: 50000, 5: 100000, 7: 200000, 14: 500000, 30: 1000000}
LEADERBOARD_REWARDS = {1: 2000000, 2: 1000000, 3: 500000}
REFERRAL_BONUS_CAP = 500000
LEADERBOARD_RESET_INTERVAL_HOURS = 24 * 7

# --- Solana Transaction Verification ---
def verify_transaction(wallet, amount):
    try:
        response = solana_client.get_confirmed_signatures_for_address2(wallet, limit=100)
        if response and 'result' in response:
            for tx in response['result']:
                if tx.get('err') is None and tx.get('memo') and int(tx.get('memo')) >= amount:
                    logging.info(f"Transaction verified for wallet {wallet} with memo amount {tx.get('memo')}.")
                    return True
        return False
    except Exception as e:
        logging.error(f"Transaction check error for {wallet}: {e}")
        return False

# --- Command Handlers ---
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    try:
        bot.reply_to(message, 
            "Welcome to MyNala Rewards Bot! 🚀\n\n"
            "Available commands:\n"
            "/verify <wallet> - Verify your wallet\n"
            "/status <wallet> - Check your status\n"
            "/buy <wallet> <amount> - Record a purchase\n"
            "/claim <wallet> - Check claimable rewards\n"
            "/referrals <wallet> - Check your referrals"
        )
    except Exception as e:
        logging.error(f"Error in welcome handler: {e}")
        bot.reply_to(message, "An error occurred. Please try again later.")

@bot.message_handler(commands=['verify'])
def verify_wallet(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /verify <wallet_address>")
            return

        wallet = parts[1]
        chat_id = message.chat.id

        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE wallet=?", (wallet,))
            result = cursor.fetchone()
            if result:
                cursor.execute("UPDATE users SET verified=1 WHERE wallet=?", (wallet,))
            else:
                now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                cursor.execute("INSERT INTO users (chat_id, wallet, verified, last_purchase) VALUES (?, ?, ?, ?)", 
                              (chat_id, wallet, 1, now))
            conn.commit()
            conn.close()
        bot.reply_to(message, f"✅ Wallet {wallet} is now verified.")
    except Exception as e:
        logging.error(f"Error in verify_wallet: {e}")
        bot.reply_to(message, "An error occurred while verifying your wallet. Please try again later.")

@bot.message_handler(commands=['status'])
def check_status(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /status <wallet_address>")
            return

        wallet = parts[1]
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT verified, streak_days, total_volume, total_rewards FROM users WHERE wallet=?", (wallet,))
            result = cursor.fetchone()
            conn.close()

        if result:
            verified, streak, volume, rewards = result
            status = "✅ Verified" if verified else "❌ Not Verified"
            bot.reply_to(message, f"📊 Status for `{wallet[:6]}...{wallet[-4:]}`:\n{status}\n📈 Volume: {volume} $MN\n🔥 Streak: {streak} days\n💰 Rewards: {rewards} $MN", parse_mode='Markdown')
        else:
            bot.reply_to(message, "Wallet not found. Please verify first.")
    except Exception as e:
        logging.error(f"Error in check_status: {e}")
        bot.reply_to(message, "An error occurred while checking your status. Please try again later.")

@bot.message_handler(commands=['claim'])
def claim_rewards(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /claim <wallet_address>")
            return

        wallet = parts[1]
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT total_rewards FROM users WHERE wallet=?", (wallet,))
            result = cursor.fetchone()
            conn.close()

        if result:
            bot.reply_to(message, f"💰 Wallet {wallet} has {result[0]} $MN in rewards.")
        else:
            bot.reply_to(message, "Wallet not found.")
    except Exception as e:
        logging.error(f"Error in claim_rewards: {e}")
        bot.reply_to(message, "An error occurred while checking your rewards. Please try again later.")

@bot.message_handler(commands=['referrals'])
def check_referrals(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /referrals <wallet_address>")
            return

        wallet = parts[1]
        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT referral_count FROM users WHERE wallet=?", (wallet,))
            result = cursor.fetchone()
            conn.close()

        if result:
            bot.reply_to(message, f"📣 Wallet {wallet} has {result[0]} referral(s).")
        else:
            bot.reply_to(message, "Wallet not found.")
    except Exception as e:
        logging.error(f"Error in check_referrals: {e}")
        bot.reply_to(message, "An error occurred while checking your referrals. Please try again later.")

@bot.message_handler(commands=['buy'])
def buy_tokens(message):
    try:
        parts = message.text.split()
        if len(parts) < 3:
            bot.reply_to(message, "Usage: /buy <wallet_address> <amount>")
            return

        wallet = parts[1]
        try:
            amount = int(parts[2])
        except ValueError:
            bot.reply_to(message, "Amount must be an integer.")
            return

        chat_id = message.chat.id
        now = datetime.now(timezone.utc)

        with db_lock:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT streak_days, last_purchase, total_volume, total_rewards FROM users WHERE wallet=?", (wallet,))
            result = cursor.fetchone()
            if result:
                streak, last_purchase, volume, rewards = result
                if last_purchase:
                    last_date = datetime.strptime(last_purchase, "%Y-%m-%d")
                    if (now.date() - last_date.date()).days == 1:
                        streak += 1
                    else:
                        streak = 1
                else:
                    streak = 1

                volume += amount
                reward = BUY_STREAK_REWARDS.get(streak, 0)
                rewards += reward

                cursor.execute("UPDATE users SET streak_days=?, last_purchase=?, total_volume=?, total_rewards=? WHERE wallet=?",
                            (streak, now.strftime("%Y-%m-%d"), volume, rewards, wallet))
                conn.commit()
                conn.close()
                bot.reply_to(message, f"✅ Buy of {amount} $MN recorded for {wallet}.\n🔥 Streak: {streak} days\n💰 Total Rewards: {rewards} $MN")
            else:
                conn.close()
                bot.reply_to(message, "Wallet not found. Please verify first.")
    except Exception as e:
        logging.error(f"Error in buy_tokens: {e}")
        bot.reply_to(message, "An error occurred while recording your purchase. Please try again later.")

# --- Webhook Management ---
def set_webhook():
    try:
        bot.remove_webhook()
        time.sleep(1)
        
        # Get the Railway-assigned URL from environment variables
        base_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
        if not base_url:
            # Fallback to the hardcoded URL if environment variable is not available
            base_url = "primary-production-cd3d.up.railway.app"
            logging.warning("RAILWAY_PUBLIC_DOMAIN not found, using hardcoded domain")
        
        # Ensure we have https:// prefix
        if not base_url.startswith("http"):
            base_url = f"https://{base_url}"
            
        # Set the webhook URL
        webhook_url = f"{base_url}/webhook/{TELEGRAM_BOT_TOKEN}"
        webhook_info = bot.set_webhook(url=webhook_url)
        
        if webhook_info:
            logging.info(f"✅ Webhook successfully set to {webhook_url}")
            return True
        else:
            logging.error(f"❌ Failed to set webhook to {webhook_url}")
            return False
    except Exception as e:
        logging.error(f"❌ Error setting webhook: {e}")
        return False

# --- Bot Startup ---
def start_bot():
    try:
        # Try to set webhook
        webhook_success = set_webhook()
        
        # If webhook setup fails, log the error but continue
        if not webhook_success:
            logging.warning("Webhook setup failed, continuing with server startup")
        
        # Log startup information
        logging.info("✅ MyNala Bot is starting...")
        print("✅ MyNala Bot is live via webhook...")
        
        # Return success status
        return webhook_success
    except Exception as e:
        logging.error(f"❌ Error starting bot: {e}")
        return False

# --- Graceful Shutdown Handler ---
def shutdown_handler():
    try:
        logging.info("Shutting down bot...")
        bot.remove_webhook()
        logging.info("Webhook removed")
    except Exception as e:
        logging.error(f"Error during shutdown: {e}")

# --- Main Entry Point ---
if __name__ == "__main__":
    # Initialize the bot
    start_success = start_bot()
    
    # Register shutdown handler
    import atexit
    atexit.register(shutdown_handler)
    
    # Get port from environment or use default
    port = int(os.environ.get("PORT", 5000))
    
    # Start the Flask server
    try:
        app.run(host="0.0.0.0", port=port)
    except Exception as e:
        logging.error(f"❌ Error starting Flask server: {e}")
