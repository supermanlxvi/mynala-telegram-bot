import os
import sqlite3
import time
import logging
from datetime import datetime, timedelta, timezone
from telebot import TeleBot, types # pyTelegramBotAPI
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
# WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL") # This variable is unused, consider removing if not needed.

# --- Define DB_FILE and LOG_FILE at the top ---
DB_FILE = "rewards.db"
LOG_FILE = "reward_bot.log" # Will be created in the current working directory of the Render service

# --- Validate Required Environment Variables ---
missing_vars = []
if not TELEGRAM_BOT_TOKEN:
    missing_vars.append("TELEGRAM_BOT_TOKEN")
if not SOLANA_RPC_URL:
    missing_vars.append("SOLANA_RPC_URL")

if missing_vars:
    # Use basic logging if full setup hasn't happened, or print
    print(f"CRITICAL: Missing environment variables: {', '.join(missing_vars)}. Exiting.")
    # logging.critical(f"❌ Critical: Missing environment variables: {', '.join(missing_vars)}. Exiting.")
    exit(1)

# --- Logging Setup ---
# Gunicorn might also provide its own logging for web requests.
# This setup adds application-specific logging.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(threadName)s - %(levelname)s - %(message)s", # Added threadName
    handlers=[
        logging.FileHandler(LOG_FILE), # Log to a file
        logging.StreamHandler()        # Log to console (visible in Render logs)
    ]
)
logger = logging.getLogger(__name__) # Create a logger instance for explicit use if needed

# --- Global Instances and App Initialization ---
app = Flask(__name__)
logger.info("Flask app instance created.")

bot = None
solana_client = None
try:
    logger.info("Attempting to initialize Telegram Bot and Solana Client...")
    bot = TeleBot(TELEGRAM_BOT_TOKEN, threaded=False) # Explicitly threaded=False; Flask handles concurrency.
    solana_client = Client(SOLANA_RPC_URL)
    logger.info("Telegram Bot and Solana Client initialized.")
except ValueError as e: # TeleBot can raise ValueError for an invalid token format
    logger.critical(f"❌ CRITICAL ERROR: Token Validation Failed: {e}", exc_info=True)
    raise
except Exception as e:
    logger.critical(f"❌ CRITICAL ERROR during bot/solana client initialization: {e}", exc_info=True)
    raise

conn = None
cursor = None
db_lock = threading.Lock() # To ensure thread-safe SQLite operations
try:
    logger.info(f"Attempting to connect to database: {DB_FILE}")
    # check_same_thread=False is needed because Flask might handle requests in different threads,
    # and this connection object might be shared or accessed by TeleBot's internal mechanisms
    # if it were to use threads (though we set threaded=False for the bot instance).
    # The db_lock provides explicit safety for our operations.
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    cursor = conn.cursor()

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY,
        verified INTEGER DEFAULT 0,
        wallet TEXT UNIQUE,
        streak_days INTEGER DEFAULT 0,
        last_purchase TEXT DEFAULT NULL, -- Store as YYYY-MM-DD string
        total_volume INTEGER DEFAULT 0,
        referral_count INTEGER DEFAULT 0,
        total_rewards INTEGER DEFAULT 0,
        referred_by TEXT DEFAULT NULL
    )
    ''')
    conn.commit()
    logger.info("Database connected and schema verified.")
except sqlite3.Error as e:
    logger.critical(f"❌ CRITICAL ERROR during database setup: {e}", exc_info=True)
    raise
except Exception as e:
    logger.critical(f"❌ CRITICAL ERROR during database setup (general): {e}", exc_info=True)
    raise

# --- Reward Constants ---
BUY_STREAK_REWARDS = {3: 50000, 5: 100000, 7: 200000, 14: 500000, 30: 1000000}
LEADERBOARD_REWARDS = {1: 2000000, 2: 1000000, 3: 500000} # Not currently used in handlers
REFERRAL_BONUS_CAP = 500000 # Not currently used in handlers
LEADERBOARD_RESET_INTERVAL_HOURS = 24 * 7 # Not currently used

# --- Solana Transaction Verification (currently unused by commands) ---
def verify_solana_transaction(wallet: str, amount: int) -> bool: # Renamed for clarity
    try:
        logger.info(f"Verifying Solana transaction for wallet {wallet} (amount check not implemented here)")
        response = solana_client.get_signatures_for_address(wallet, limit=10)
        
        if response and response.get('result'): # Check if 'result' key exists and is not empty
            logger.info(f"Recent transactions found for wallet {wallet}. Further verification (amount/memo) would require fetching full transaction details.")
            return True # Basic check: activity found
        logger.info(f"No recent transactions found for wallet {wallet} or RPC response is empty/malformed.")
        return False
    except RPCException as e:
        logger.error(f"Solana RPC error during transaction check for {wallet}: {e}", exc_info=True)
        return False
    except Exception as e:
        logger.error(f"General error during transaction check for {wallet}: {e}", exc_info=True)
        return False

# --- Webhook Management Function ---
def set_webhook_on_startup():
    logger.info("Webhook setup initiated on startup thread.")
    time.sleep(5) # Give Flask app/Gunicorn time to fully bind and start

    try:
        logger.info("Attempting to remove existing webhook...")
        bot.remove_webhook()
        logger.info("Existing webhook removed (or no webhook was set).")
        time.sleep(1) # Brief pause

        base_url = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
        if not base_url:
            logger.critical("❌ CRITICAL ERROR: RENDER_EXTERNAL_HOSTNAME environment variable is not set. Cannot set webhook.")
            return False

        if not base_url.startswith("https://"):
            base_url = f"https://{base_url}"
            logger.info(f"RENDER_EXTERNAL_HOSTNAME does not start with https. Prepended: {base_url}")


        webhook_url_path = f"/webhook/{TELEGRAM_BOT_TOKEN}"
        full_webhook_url = f"{base_url.rstrip('/')}{webhook_url_path}" # Ensure no double slashes
        
        logger.info(f"Attempting to set webhook to {full_webhook_url}")
        
        # `allowed_updates` can be specified to only receive certain update types
        result = bot.set_webhook(url=full_webhook_url) #, allowed_updates=['message'])
        
        if result:
            logger.info(f"✅ Webhook successfully set to {full_webhook_url}")
            # Optionally verify webhook
            # webhook_info = bot.get_webhook_info()
            # logger.info(f"Webhook info from Telegram: {webhook_info}")
            return True
        else:
            logger.error(f"❌ Failed to set webhook to {full_webhook_url}. Result: {result}")
            # webhook_info = bot.get_webhook_info() # Check what Telegram thinks about the webhook
            # logger.error(f"Webhook info from Telegram after failed set attempt: {webhook_info}")
            return False
    except Exception as e:
        logger.critical(f"❌ Exception in set_webhook_on_startup: {e}", exc_info=True)
        return False

# --- Flask Routes ---
@app.route("/", methods=["GET"])
def index():
    logger.info("Received GET request to /")
    return "✅ MyNala Bot is running with TeleBot and Flask!", 200

@app.route("/health", methods=["GET"])
def health_check(): # Renamed for clarity
    logger.info("Received GET request to /health")
    # Basic health check, can be expanded (e.g., check DB connection, Solana client ping)
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}), 200

@app.route(f"/webhook/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def webhook_handler(): # Renamed for clarity
    if request.headers.get('content-type') == 'application/json':
        try:
            json_string = request.get_data().decode("utf-8")
            update = types.Update.de_json(json_string)

            logger.info(f"Webhook received Update ID: {update.update_id}")
            if update.message:
                logger.info(f"  Type: Message, Chat ID: {update.message.chat.id}, User: {update.message.from_user.username if update.message.from_user else 'N/A'}, Text: '{update.message.text}'")
            elif update.edited_message:
                logger.info(f"  Type: Edited Message, Chat ID: {update.edited_message.chat.id}, Text: '{update.edited_message.text}'")
            # Add other update types if needed (callback_query, etc.)
            else:
                logger.info(f"  Type: Other (not a standard message). Update keys: {update.to_dict().keys()}")

            bot.process_new_updates([update])
            # logger.info(f"Webhook Update ID {update.update_id} passed to bot.process_new_updates.")
            # Note: "Successfully processed" is implicitly true if no exception bubbles up from process_new_updates.
            # The actual success of command execution will be logged by the handlers.
            return "OK", 200
        except Exception as e:
            logger.error(f"❌ Error processing Telegram update in webhook_handler: {e}", exc_info=True)
            return "Error processing update", 500 # Signal error to Telegram
    else:
        logger.warning(f"Webhook received request with invalid content type: {request.headers.get('content-type')}")
        return "Content-Type must be application/json", 400

# --- Telegram Bot Command Handlers ---

def _safe_reply_to(message, text, **kwargs):
    """Helper function to safely send replies and log issues."""
    chat_id = message.chat.id
    command_text = message.text.split()[0] if message.text and message.text.startswith('/') else "N/A"
    logger.info(f"ChatID {chat_id} ({command_text}): Attempting to send reply. Markdown: {kwargs.get('parse_mode') == 'Markdown'}. Text: '''{text[:200]}...'''") # Log part of text
    try:
        bot.reply_to(message, text, **kwargs)
        logger.info(f"ChatID {chat_id} ({command_text}): Successfully called bot.reply_to.")
    except Exception as e:
        logger.error(f"ChatID {chat_id} ({command_text}): Error during bot.reply_to. Text: '''{text[:200]}...'''. Error: {e}", exc_info=True)
        # Fallback for Markdown errors
        if kwargs.get('parse_mode') == 'Markdown':
            logger.warning(f"ChatID {chat_id} ({command_text}): Markdown reply failed. Attempting plain text fallback.")
            try:
                plain_text = f"Error displaying formatted message. Original content was intended to be: {text}" # Simplistic fallback
                bot.reply_to(message, plain_text) # Try without parse_mode
                logger.info(f"ChatID {chat_id} ({command_text}): Plain text fallback reply sent.")
            except Exception as e_fallback:
                logger.error(f"ChatID {chat_id} ({command_text}): Error during plain text fallback bot.reply_to: {e_fallback}", exc_info=True)

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    chat_id = message.chat.id
    logger.info(f"ChatID {chat_id}: Received /start or /help command.")
    welcome_text = (
        "Welcome to MyNala Rewards Bot! 🚀\n\n"
        "Available commands:\n"
        "/ping - Check if the bot is responsive.\n"
        "/verify <wallet> - Verify your wallet and link it to your Telegram chat ID.\n"
        "/status <wallet> - Check your current rewards status, streak, and volume.\n"
        "/buy <wallet> <amount> - Record a purchase and update your streak/volume.\n"
        "/claim <wallet> - View your total claimable rewards.\n"
        "/referrals <wallet> - Check your referral count.\n" # Added comma here
        "/leaderboard - View top users." # New command
    )
    _safe_reply_to(message, welcome_text)

@bot.message_handler(commands=['ping'])
def ping_command(message):
    chat_id = message.chat.id
    logger.info(f"ChatID {chat_id}: Received /ping command.")
    _safe_reply_to(message, "Pong! I am here. ✅")

@bot.message_handler(commands=['leaderboard'])
def send_leaderboard(message):
    chat_id = message.chat.id
    logger.info(f"ChatID {chat_id}: Received /leaderboard command.")

    limit = 5  # Top 5 users
    leaderboard_sections = []

    with db_lock:
        try:
            # --- Top 5 by Total Volume ---
            logger.info(f"ChatID {chat_id}: Fetching top {limit} users by total_volume.")
            cursor.execute("SELECT wallet, total_volume FROM users WHERE verified=1 ORDER BY total_volume DESC LIMIT ?", (limit,))
            top_volume_users = cursor.fetchall()
            
            volume_leaderboard = f"🏆 *Top {limit} by Volume ($MN)* 🏆\n"
            if top_volume_users:
                for i, user_data in enumerate(top_volume_users):
                    wallet_short = f"{user_data[0][:6]}...{user_data[0][-4:]}" if user_data[0] else "N/A"
                    volume_leaderboard += f"{i+1}. `{wallet_short}` - {user_data[1]:,} $MN\n"
            else:
                volume_leaderboard += "_No users with volume found._\n"
            leaderboard_sections.append(volume_leaderboard)

            # --- Top 5 by Total Rewards ---
            logger.info(f"ChatID {chat_id}: Fetching top {limit} users by total_rewards.")
            cursor.execute("SELECT wallet, total_rewards FROM users WHERE verified=1 ORDER BY total_rewards DESC LIMIT ?", (limit,))
            top_rewards_users = cursor.fetchall()

            rewards_leaderboard = f"💰 *Top {limit} by Rewards ($MN)* 💰\n"
            if top_rewards_users:
                for i, user_data in enumerate(top_rewards_users):
                    wallet_short = f"{user_data[0][:6]}...{user_data[0][-4:]}" if user_data[0] else "N/A"
                    rewards_leaderboard += f"{i+1}. `{wallet_short}` - {user_data[1]:,} $MN\n"
            else:
                rewards_leaderboard += "_No users with rewards found._\n"
            leaderboard_sections.append(rewards_leaderboard)

            # --- Top 5 by Referral Count ---
            logger.info(f"ChatID {chat_id}: Fetching top {limit} users by referral_count.")
            cursor.execute("SELECT wallet, referral_count FROM users WHERE verified=1 ORDER BY referral_count DESC LIMIT ?", (limit,))
            top_referral_users = cursor.fetchall()

            referral_leaderboard = f"📣 *Top {limit} by Referrals* 📣\n"
            if top_referral_users:
                for i, user_data in enumerate(top_referral_users):
                    wallet_short = f"{user_data[0][:6]}...{user_data[0][-4:]}" if user_data[0] else "N/A"
                    referral_leaderboard += f"{i+1}. `{wallet_short}` - {user_data[1]} referrals\n"
            else:
                referral_leaderboard += "_No users with referrals found._\n"
            leaderboard_sections.append(referral_leaderboard)

            # Combine all sections
            full_leaderboard_message = "\n\n".join(leaderboard_sections)
            if not full_leaderboard_message.strip(): # Should not happen if sections always have headers
                full_leaderboard_message = "Leaderboard data is currently unavailable."
            
            _safe_reply_to(message, full_leaderboard_message, parse_mode='Markdown')

        except sqlite3.Error as e:
            logger.error(f"ChatID {chat_id}: Database error during /leaderboard: {e}", exc_info=True)
            _safe_reply_to(message, "An error occurred while fetching the leaderboard. Please try again.")
        except Exception as e:
            logger.error(f"ChatID {chat_id}: General error during /leaderboard: {e}", exc_info=True)
            _safe_reply_to(message, "An unexpected error occurred while fetching the leaderboard.")



@bot.message_handler(commands=['verify'])
def verify_wallet(message):
    chat_id = message.chat.id
    logger.info(f"ChatID {chat_id}: Received /verify command. Full text: '{message.text}'")
    parts = message.text.split()
    
    if len(parts) < 2:
        _safe_reply_to(message, "Usage: /verify <wallet_address>")
        return

    wallet = parts[1].strip()
    if not wallet: # Basic validation for wallet format can be added here
        _safe_reply_to(message, "Wallet address cannot be empty. Please provide a valid Solana wallet address.")
        return
    
    logger.info(f"ChatID {chat_id}: Processing /verify for wallet: {wallet}")
    with db_lock:
        try:
            cursor.execute("SELECT chat_id, verified FROM users WHERE wallet=?", (wallet,))
            result = cursor.fetchone()
            reply_made = False

            if result:
                existing_chat_id, current_verified_status = result
                if existing_chat_id == chat_id:
                    if current_verified_status:
                        _safe_reply_to(message, f"✅ Wallet {wallet[:6]}...{wallet[-4:]} is already verified and linked to this account.")
                    else: # Wallet exists, linked to this chat_id, but not verified (shouldn't happen if insert requires verify=1)
                        cursor.execute("UPDATE users SET verified=1 WHERE wallet=? AND chat_id=?", (wallet, chat_id))
                        conn.commit()
                        _safe_reply_to(message, f"✅ Wallet {wallet[:6]}...{wallet[-4:]} is now re-verified and linked to your account.")
                else: # Wallet exists but linked to a different chat_id
                    _safe_reply_to(message, f"❌ Wallet {wallet[:6]}...{wallet[-4:]} is already linked to another Telegram account.")
            else: # New wallet
                now_utc_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                # For a new wallet, verification implies it's associated with this chat_id
                cursor.execute("INSERT INTO users (chat_id, wallet, verified, last_purchase) VALUES (?, ?, 1, ?)",
                               (chat_id, wallet, now_utc_str)) # Ensure verified is 1
                conn.commit()
                _safe_reply_to(message, f"✅ Wallet {wallet[:6]}...{wallet[-4:]} is now verified and linked to your account.")
            
        except sqlite3.IntegrityError as e: # Handles UNIQUE constraint violation for wallet
            logger.error(f"ChatID {chat_id}: Database integrity error during /verify for wallet {wallet}: {e}", exc_info=False) # No need for full trace for common integrity error
            _safe_reply_to(message, f"❌ This wallet address ({wallet[:6]}...{wallet[-4:]}) might already be registered or there's a conflict. If you believe this is an error, contact support.")
        except sqlite3.Error as e:
            logger.error(f"ChatID {chat_id}: Database error during /verify for wallet {wallet}: {e}", exc_info=True)
            _safe_reply_to(message, "An error occurred while processing your verification. Please try again.")
        except Exception as e:
            logger.error(f"ChatID {chat_id}: General error during /verify for wallet {wallet}: {e}", exc_info=True)
            _safe_reply_to(message, "An unexpected error occurred during verification. Please try again.")


@bot.message_handler(commands=['status'])
def check_status(message):
    chat_id = message.chat.id
    logger.info(f"ChatID {chat_id}: Received /status command. Full text: '{message.text}'")
    parts = message.text.split()

    if len(parts) < 2:
        _safe_reply_to(message, "Usage: /status <wallet_address>")
        return

    wallet = parts[1].strip()
    if not wallet:
        _safe_reply_to(message, "Wallet address cannot be empty.")
        return

    logger.info(f"ChatID {chat_id}: Processing /status for wallet: {wallet}")
    with db_lock:
        try:
            cursor.execute("SELECT verified, streak_days, total_volume, total_rewards FROM users WHERE wallet=?", (wallet,))
            result = cursor.fetchone()

            if result:
                verified, streak, volume, rewards = result
                status_msg = "✅ Verified" if verified else "❌ Not Verified (use /verify)"
                
                reply_text = (
                    f"📊 Status for `{wallet[:6]}...{wallet[-4:]}`:\n"
                    f"Verification: {status_msg}\n"
                    f"📈 Volume: {volume} $MN\n"
                    f"🔥 Streak: {streak} days\n"
                    f"💰 Total Rewards: {rewards} $MN"
                )
                _safe_reply_to(message, reply_text, parse_mode='Markdown')
            else:
                _safe_reply_to(message, "Wallet not found. Please verify it first using /verify <wallet_address>.")
        
        except sqlite3.Error as e:
            logger.error(f"ChatID {chat_id}: Database error during /status for wallet {wallet}: {e}", exc_info=True)
            _safe_reply_to(message, "An error occurred while fetching your status. Please try again.")
        except Exception as e:
            logger.error(f"ChatID {chat_id}: General error during /status for wallet {wallet}: {e}", exc_info=True)
            _safe_reply_to(message, "An unexpected error occurred while fetching status. Please try again.")


@bot.message_handler(commands=['claim'])
def claim_rewards(message):
    chat_id = message.chat.id
    logger.info(f"ChatID {chat_id}: Received /claim command. Full text: '{message.text}'")
    parts = message.text.split()
    if len(parts) < 2:
        _safe_reply_to(message, "Usage: /claim <wallet_address>")
        return

    wallet = parts[1].strip()
    if not wallet:
        _safe_reply_to(message, "Wallet address cannot be empty.")
        return

    logger.info(f"ChatID {chat_id}: Processing /claim for wallet: {wallet}")
    with db_lock:
        try:
            cursor.execute("SELECT total_rewards, verified FROM users WHERE wallet=?", (wallet,))
            result = cursor.fetchone()

            if result:
                total_rewards_val, verified_status = result
                if not verified_status:
                    _safe_reply_to(message, f"⚠️ Wallet `{wallet[:6]}...{wallet[-4:]}` is not verified. Please use /verify first. Current rewards (unclaimable): {total_rewards_val} $MN", parse_mode='Markdown')
                else:
                     _safe_reply_to(message, f"💰 Wallet `{wallet[:6]}...{wallet[-4:]}` has {total_rewards_val} $MN in rewards. Claiming functionality is processed separately.", parse_mode='Markdown')
            else:
                _safe_reply_to(message, "Wallet not found. Please use /verify first.")
        except sqlite3.Error as e:
            logger.error(f"ChatID {chat_id}: Database error during /claim for wallet {wallet}: {e}", exc_info=True)
            _safe_reply_to(message, "An error occurred while checking your claimable rewards.")
        except Exception as e:
            logger.error(f"ChatID {chat_id}: General error during /claim for wallet {wallet}: {e}", exc_info=True)
            _safe_reply_to(message, "An unexpected error occurred while checking claims.")


@bot.message_handler(commands=['leaderboard'])
def show_leaderboard(message):
    chat_id = message.chat.id
    logger.info(f"ChatID {chat_id}: Received /leaderboard command.")

    leaderboard_text_parts = ["🏆 MyNala Leaderboards 🏆\n"]

    with db_lock:
        try:
            # --- Top 5 by Total Volume ---
            leaderboard_text_parts.append("\n--- 🚀 Top 5 by Volume ($MN) ---")
            cursor.execute("SELECT wallet, total_volume FROM users WHERE verified=1 ORDER BY total_volume DESC LIMIT 5")
            top_volume_users = cursor.fetchall()
            if top_volume_users:
                for i, (wallet, volume) in enumerate(top_volume_users):
                    leaderboard_text_parts.append(f"{i+1}. `{wallet[:6]}...{wallet[-4:]}` - {volume:,} $MN")
            else:
                leaderboard_text_parts.append("No users with volume yet.")

            # --- Top 5 by Total Rewards ---
            leaderboard_text_parts.append("\n--- 💰 Top 5 by Rewards ($MN) ---")
            cursor.execute("SELECT wallet, total_rewards FROM users WHERE verified=1 ORDER BY total_rewards DESC LIMIT 5")
            top_rewards_users = cursor.fetchall()
            if top_rewards_users:
                for i, (wallet, rewards) in enumerate(top_rewards_users):
                    leaderboard_text_parts.append(f"{i+1}. `{wallet[:6]}...{wallet[-4:]}` - {rewards:,} $MN")
            else:
                leaderboard_text_parts.append("No users with rewards yet.")

            # --- Top 5 by Referral Count ---
            leaderboard_text_parts.append("\n--- 📣 Top 5 by Referrals ---")
            cursor.execute("SELECT wallet, referral_count FROM users WHERE verified=1 ORDER BY referral_count DESC LIMIT 5")
            top_referral_users = cursor.fetchall()
            if top_referral_users:
                for i, (wallet, referrals) in enumerate(top_referral_users):
                    leaderboard_text_parts.append(f"{i+1}. `{wallet[:6]}...{wallet[-4:]}` - {referrals} referrals")
            else:
                leaderboard_text_parts.append("No users with referrals yet.")
            
            final_leaderboard_text = "\n".join(leaderboard_text_parts)
            _safe_reply_to(message, final_leaderboard_text, parse_mode='Markdown')

        except sqlite3.Error as e:
            logger.error(f"ChatID {chat_id}: Database error during /leaderboard: {e}", exc_info=True)
            _safe_reply_to(message, "An error occurred while fetching the leaderboards. Please try again.")
        except Exception as e:
            logger.error(f"ChatID {chat_id}: General error during /leaderboard: {e}", exc_info=True)
            _safe_reply_to(message, "An unexpected error occurred while fetching the leaderboards.")


@bot.message_handler(commands=['referrals'])
def check_referrals(message):
    chat_id = message.chat.id
    logger.info(f"ChatID {chat_id}: Received /referrals command. Full text: '{message.text}'")
    parts = message.text.split()
    if len(parts) < 2:
        _safe_reply_to(message, "Usage: /referrals <wallet_address>")
        return

    wallet = parts[1].strip()
    if not wallet:
        _safe_reply_to(message, "Wallet address cannot be empty.")
        return
    
    logger.info(f"ChatID {chat_id}: Processing /referrals for wallet: {wallet}")
    with db_lock:
        try:
            cursor.execute("SELECT referral_count, verified FROM users WHERE wallet=?", (wallet,))
            result = cursor.fetchone()

            if result:
                ref_count, verified_status = result
                if not verified_status:
                     _safe_reply_to(message, f"⚠️ Wallet `{wallet[:6]}...{wallet[-4:]}` is not verified. Please use /verify first. Referral count: {ref_count}.", parse_mode='Markdown')
                else:
                    _safe_reply_to(message, f"📣 Wallet `{wallet[:6]}...{wallet[-4:]}` has {ref_count} referral(s).", parse_mode='Markdown')
            else:
                _safe_reply_to(message, "Wallet not found. Please use /verify first.")
        except sqlite3.Error as e:
            logger.error(f"ChatID {chat_id}: Database error during /referrals for wallet {wallet}: {e}", exc_info=True)
            _safe_reply_to(message, "An error occurred while checking your referrals.")
        except Exception as e:
            logger.error(f"ChatID {chat_id}: General error during /referrals for wallet {wallet}: {e}", exc_info=True)
            _safe_reply_to(message, "An unexpected error occurred while checking referrals.")


@bot.message_handler(commands=['buy'])
def buy_tokens(message):
    chat_id = message.chat.id
    logger.info(f"ChatID {chat_id}: Received /buy command. Full text: '{message.text}'")
    parts = message.text.split()
    if len(parts) < 3:
        _safe_reply_to(message, "Usage: /buy <wallet_address> <amount_MN_tokens>")
        return

    wallet = parts[1].strip()
    if not wallet:
        _safe_reply_to(message, "Wallet address cannot be empty.")
        return

    try:
        amount = int(parts[2])
        if amount <= 0:
            _safe_reply_to(message, "Amount must be a positive integer.")
            return
    except ValueError:
        _safe_reply_to(message, "Amount must be a valid integer (e.g., 1000).")
        return

    logger.info(f"ChatID {chat_id}: Processing /buy for wallet {wallet}, amount {amount}")
    
    # Note: Actual Solana transaction verification for the buy isn't implemented here.
    # This command currently *records* a buy based on user input.
    # You would need a robust way to confirm the buy on-chain if this is for actual rewards.
    # For now, we assume the user is honestly reporting a buy for tracking purposes.

    now_utc = datetime.now(timezone.utc)
    today_utc_str = now_utc.strftime("%Y-%m-%d")

    with db_lock:
        try:
            cursor.execute("SELECT verified, streak_days, last_purchase, total_volume, total_rewards FROM users WHERE wallet=?", (wallet,))
            result = cursor.fetchone()

            if result:
                verified_status, current_streak, last_purchase_str, current_volume, current_rewards = result

                if not verified_status:
                    _safe_reply_to(message, f"⚠️ Wallet `{wallet[:6]}...{wallet[-4:]}` is not verified. Please use /verify first before recording buys.", parse_mode='Markdown')
                    return

                new_streak = current_streak
                if last_purchase_str:
                    last_purchase_date = datetime.strptime(last_purchase_str, "%Y-%m-%d").date()
                    days_diff = (now_utc.date() - last_purchase_date).days
                    if days_diff == 1: # Purchase on consecutive day
                        new_streak += 1
                    elif days_diff > 1: # Streak broken
                        new_streak = 1 # Reset to 1 for today's purchase
                    # If days_diff == 0 (same day purchase), streak doesn't change yet, but purchase updates.
                    # If days_diff < 0 (last_purchase in future), data error, treat as streak reset.
                    elif days_diff < 0:
                        new_streak = 1
                        logger.warning(f"ChatID {chat_id}: last_purchase_date for wallet {wallet} was in the future ({last_purchase_str}). Resetting streak.")

                else: # No previous purchase, this is the first day of the streak
                    new_streak = 1
                
                new_volume = current_volume + amount
                streak_reward_gained = BUY_STREAK_REWARDS.get(new_streak, 0) # Reward for *achieving* this new_streak
                new_total_rewards = current_rewards + streak_reward_gained

                cursor.execute(
                    "UPDATE users SET streak_days=?, last_purchase=?, total_volume=?, total_rewards=? WHERE wallet=?",
                    (new_streak, today_utc_str, new_volume, new_total_rewards, wallet)
                )
                conn.commit()
                
                reply_parts = [
                    f"✅ Buy of {amount} $MN recorded for `{wallet[:6]}...{wallet[-4:]}`.",
                    f"🔥 New Streak: {new_streak} days.",
                    f"📈 New Total Volume: {new_volume} $MN."
                ]
                if streak_reward_gained > 0:
                    reply_parts.append(f"🎉 Streak Reward: +{streak_reward_gained} $MN for reaching {new_streak} days!")
                reply_parts.append(f"💰 New Total Rewards: {new_total_rewards} $MN.")
                
                _safe_reply_to(message, "\n".join(reply_parts), parse_mode='Markdown')

            else:
                _safe_reply_to(message, "Wallet not found. Please use /verify <wallet_address> first to register your wallet.")
        
        except sqlite3.Error as e:
            logger.error(f"ChatID {chat_id}: Database error during /buy for wallet {wallet}, amount {amount}: {e}", exc_info=True)
            _safe_reply_to(message, "An error occurred while recording your purchase. Please try again.")
        except Exception as e:
            logger.error(f"ChatID {chat_id}: General error during /buy for wallet {wallet}, amount {amount}: {e}", exc_info=True)
            _safe_reply_to(message, "An unexpected error occurred while recording your purchase. Please try again.")


# --- Main Gunicorn Entry Point and Webhook Thread ---
if __name__ == "__main__":
    # This block is useful for local testing WITHOUT Gunicorn.
    # For Render, Gunicorn calls `app` directly.
    logger.info("Starting Flask app directly (for local testing without Gunicorn). Webhook will NOT be set automatically here.")
    # To test webhook locally, you'd need a tool like ngrok and manually set the webhook
    # OR run the set_webhook_on_startup function after ngrok is running and provides a URL.
    # Example: If you had ngrok running:
    # bot.remove_webhook()
    # time.sleep(1)
    # NGROK_URL = "https_your_ngrok_url.ngrok.io" # Replace with your ngrok URL
    # bot.set_webhook(url=f"{NGROK_URL}/webhook/{TELEGRAM_BOT_TOKEN}")
    # logger.info(f"Local webhook set to {NGROK_URL}/webhook/{TELEGRAM_BOT_TOKEN}")
    
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
    # When running with Gunicorn, the webhook setup thread below is more relevant.

else: # This block will be executed when Gunicorn imports the file
    try:
        logger.info("Spawning thread for webhook setup (Gunicorn environment)...")
        webhook_thread = threading.Thread(target=set_webhook_on_startup, name="WebhookSetupThread")
        webhook_thread.daemon = True # Allow main program to exit even if thread is running
        webhook_thread.start()
        logger.info("Webhook setup thread started.")
    except Exception as e:
        logger.critical(f"❌ CRITICAL ERROR: Could not spawn webhook setup thread: {e}", exc_info=True)