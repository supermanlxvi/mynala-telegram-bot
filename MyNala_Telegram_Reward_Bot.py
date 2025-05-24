import os
import sqlite3
import time
import logging
import json
from datetime import datetime, timedelta, timezone
from telebot import TeleBot, types
from solana.rpc.api import Client
import threading
from dotenv import load_dotenv
from flask import Flask, request, jsonify

# --- Load environment variables from .env FIRST ---
load_dotenv()

# Environment variables with fallbacks for Railway
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
RAILWAY_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN")  # Set this in Railway
PORT = int(os.environ.get("PORT", 5000))

# Database and logging
DB_FILE = "rewards.db"
LOG_FILE = "reward_bot.log"

# Validate required environment variables
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()  # Also log to console for Railway logs
    ]
)

logger = logging.getLogger(__name__)

# --- Global Instances ---
bot = TeleBot(TELEGRAM_BOT_TOKEN)
solana_client = Client(SOLANA_RPC_URL) if SOLANA_RPC_URL else None

# --- Flask App for Webhook ---
app = Flask(__name__)

# Health check endpoint for Railway
@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()}), 200

# Root endpoint
@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "message": "MyNala Telegram Bot is running",
        "status": "active",
        "webhook_path": f"/{TELEGRAM_BOT_TOKEN}"
    }), 200

# Webhook endpoint for Telegram
@app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        json_string = request.get_data().decode("utf-8")
        update = types.Update.de_json(json.loads(json_string))
        bot.process_new_updates([update])
        logger.info("Webhook processed successfully")
        return "OK", 200
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        return "Error", 500

# --- Database Setup ---
db_lock = threading.Lock()

def init_database():
    """Initialize the database with proper error handling"""
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        cursor = conn.cursor()
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            verified INTEGER DEFAULT 0,
            wallet TEXT UNIQUE,
            streak_days INTEGER DEFAULT 0,
            last_purchase TEXT DEFAULT NULL,
            total_volume INTEGER DEFAULT 0,
            referral_count INTEGER DEFAULT 0,
            total_rewards INTEGER DEFAULT 0,
            referred_by TEXT DEFAULT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        # Create indexes for better performance
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_wallet ON users(wallet)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_chat_id ON users(chat_id)')
        
        conn.commit()
        logger.info("Database initialized successfully")
        return conn
    except Exception as e:
        logger.error(f"Database initialization error: {e}")
        raise

# Initialize database connection
conn = init_database()
cursor = conn.cursor()

# --- Reward Constants ---
BUY_STREAK_REWARDS = {3: 50000, 5: 100000, 7: 200000, 14: 500000, 30: 1000000}
LEADERBOARD_REWARDS = {1: 2000000, 2: 1000000, 3: 500000}
REFERRAL_BONUS_CAP = 500000
LEADERBOARD_RESET_INTERVAL_HOURS = 24 * 7

# --- Utility Functions ---
def is_valid_wallet(wallet_address):
    """Basic validation for Solana wallet address"""
    if not wallet_address or len(wallet_address) < 32 or len(wallet_address) > 44:
        return False
    # Basic character validation (base58)
    valid_chars = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    return all(c in valid_chars for c in wallet_address)

def get_user_by_wallet(wallet):
    """Get user data by wallet address"""
    try:
        with db_lock:
            cursor.execute("SELECT * FROM users WHERE wallet=?", (wallet,))
            return cursor.fetchone()
    except Exception as e:
        logger.error(f"Database query error: {e}")
        return None

def update_user_timestamp(wallet):
    """Update the last modified timestamp for a user"""
    try:
        with db_lock:
            cursor.execute(
                "UPDATE users SET updated_at=? WHERE wallet=?",
                (datetime.now().isoformat(), wallet)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Timestamp update error: {e}")

# --- Solana Transaction Verification ---
def verify_transaction(wallet, amount):
    """Verify Solana transaction with improved error handling"""
    if not solana_client:
        logger.warning("Solana client not initialized")
        return False
    
    try:
        response = solana_client.get_confirmed_signatures_for_address2(wallet, limit=100)
        if response and 'result' in response:
            for tx in response['result']:
                if tx.get('err') is None and tx.get('memo'):
                    try:
                        memo_amount = int(tx.get('memo'))
                        if memo_amount >= amount:
                            logger.info(f"Transaction verified for wallet {wallet} with memo amount {memo_amount}")
                            return True
                    except (ValueError, TypeError):
                        continue
        return False
    except Exception as e:
        logger.error(f"Transaction verification error for {wallet}: {e}")
        return False

# --- Command Handlers ---
@bot.message_handler(commands=['start'])
def start_command(message):
    """Welcome message with instructions"""
    welcome_text = """
🎉 Welcome to MyNala Rewards Bot!

Available commands:
• /verify <wallet_address> - Verify your wallet
• /status <wallet_address> - Check your status
• /claim <wallet_address> - Check claimable rewards
• /referrals <wallet_address> - Check referral count
• /buy <wallet_address> <amount> - Record a purchase
• /help - Show this help message

Get started by verifying your wallet with /verify command!
    """
    bot.reply_to(message, welcome_text.strip())

@bot.message_handler(commands=['help'])
def help_command(message):
    """Show help information"""
    start_command(message)

@bot.message_handler(commands=['verify'])
def verify_wallet(message):
    """Verify a wallet address"""
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "❌ Usage: /verify <wallet_address>")
        return

    wallet = parts[1].strip()
    chat_id = message.chat.id

    if not is_valid_wallet(wallet):
        bot.reply_to(message, "❌ Invalid wallet address format")
        return

    try:
        with db_lock:
            cursor.execute("SELECT * FROM users WHERE wallet=?", (wallet,))
            result = cursor.fetchone()
            
            now = datetime.now().isoformat()
            
            if result:
                cursor.execute(
                    "UPDATE users SET verified=1, chat_id=?, updated_at=? WHERE wallet=?",
                    (chat_id, now, wallet)
                )
                logger.info(f"Updated existing wallet verification: {wallet}")
            else:
                cursor.execute(
                    """INSERT INTO users 
                       (chat_id, wallet, verified, last_purchase, created_at, updated_at) 
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (chat_id, wallet, 1, None, now, now)
                )
                logger.info(f"New wallet verified: {wallet}")
            
            conn.commit()
        
        bot.reply_to(message, f"✅ Wallet `{wallet[:6]}...{wallet[-4:]}` is now verified!", parse_mode='Markdown')
        
    except sqlite3.IntegrityError:
        bot.reply_to(message, "❌ This wallet is already registered")
    except Exception as e:
        logger.error(f"Verification error: {e}")
        bot.reply_to(message, "❌ An error occurred during verification. Please try again.")

@bot.message_handler(commands=['status'])
def check_status(message):
    """Check wallet status"""
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "❌ Usage: /status <wallet_address>")
        return

    wallet = parts[1].strip()
    
    try:
        with db_lock:
            cursor.execute(
                "SELECT verified, streak_days, total_volume, total_rewards, referral_count FROM users WHERE wallet=?",
                (wallet,)
            )
            result = cursor.fetchone()

        if result:
            verified, streak, volume, rewards, referrals = result
            status = "✅ Verified" if verified else "❌ Not Verified"
            
            status_text = f"""
📊 Status for `{wallet[:6]}...{wallet[-4:]}`:
{status}
📈 Volume: {volume:,} $MN
🔥 Streak: {streak} days
💰 Rewards: {rewards:,} $MN
👥 Referrals: {referrals}
            """.strip()
            
            bot.reply_to(message, status_text, parse_mode='Markdown')
        else:
            bot.reply_to(message, "❌ Wallet not found. Please verify first with /verify")
            
    except Exception as e:
        logger.error(f"Status check error: {e}")
        bot.reply_to(message, "❌ An error occurred. Please try again.")

@bot.message_handler(commands=['claim'])
def claim_rewards(message):
    """Check claimable rewards"""
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "❌ Usage: /claim <wallet_address>")
        return

    wallet = parts[1].strip()
    
    try:
        with db_lock:
            cursor.execute("SELECT total_rewards, verified FROM users WHERE wallet=?", (wallet,))
            result = cursor.fetchone()

        if result:
            rewards, verified = result
            if not verified:
                bot.reply_to(message, "❌ Wallet not verified. Use /verify first.")
                return
                
            bot.reply_to(message, f"💰 Wallet `{wallet[:6]}...{wallet[-4:]}` has {rewards:,} $MN in rewards available!", parse_mode='Markdown')
        else:
            bot.reply_to(message, "❌ Wallet not found. Please verify first with /verify")
            
    except Exception as e:
        logger.error(f"Claim check error: {e}")
        bot.reply_to(message, "❌ An error occurred. Please try again.")

@bot.message_handler(commands=['referrals'])
def check_referrals(message):
    """Check referral count"""
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "❌ Usage: /referrals <wallet_address>")
        return

    wallet = parts[1].strip()
    
    try:
        with db_lock:
            cursor.execute("SELECT referral_count, verified FROM users WHERE wallet=?", (wallet,))
            result = cursor.fetchone()

        if result:
            referrals, verified = result
            if not verified:
                bot.reply_to(message, "❌ Wallet not verified. Use /verify first.")
                return
                
            bot.reply_to(message, f"👥 Wallet `{wallet[:6]}...{wallet[-4:]}` has {referrals} referral(s)!", parse_mode='Markdown')
        else:
            bot.reply_to(message, "❌ Wallet not found. Please verify first with /verify")
            
    except Exception as e:
        logger.error(f"Referrals check error: {e}")
        bot.reply_to(message, "❌ An error occurred. Please try again.")

@bot.message_handler(commands=['buy'])
def buy_tokens(message):
    """Record a token purchase"""
    parts = message.text.split()
    if len(parts) < 3:
        bot.reply_to(message, "❌ Usage: /buy <wallet_address> <amount>")
        return

    wallet = parts[1].strip()
    try:
        amount = int(parts[2])
        if amount <= 0:
            bot.reply_to(message, "❌ Amount must be positive")
            return
    except ValueError:
        bot.reply_to(message, "❌ Amount must be a valid number")
        return

    now = datetime.now(timezone.utc)
    
    try:
        with db_lock:
            cursor.execute(
                "SELECT streak_days, last_purchase, total_volume, total_rewards, verified FROM users WHERE wallet=?",
                (wallet,)
            )
            result = cursor.fetchone()
            
            if not result:
                bot.reply_to(message, "❌ Wallet not found. Please verify first with /verify")
                return
                
            streak, last_purchase, volume, rewards, verified = result
            
            if not verified:
                bot.reply_to(message, "❌ Wallet not verified. Use /verify first.")
                return
            
            # Calculate streak
            if last_purchase:
                try:
                    last_date = datetime.fromisoformat(last_purchase.replace('Z', '+00:00'))
                    days_diff = (now.date() - last_date.date()).days
                    
                    if days_diff == 1:
                        streak += 1
                    elif days_diff == 0:
                        # Same day purchase, don't reset streak
                        pass
                    else:
                        streak = 1
                except (ValueError, AttributeError):
                    streak = 1
            else:
                streak = 1

            # Update totals
            volume += amount
            reward = BUY_STREAK_REWARDS.get(streak, 0)
            rewards += reward

            # Update database
            cursor.execute(
                """UPDATE users SET 
                   streak_days=?, last_purchase=?, total_volume=?, total_rewards=?, updated_at=? 
                   WHERE wallet=?""",
                (streak, now.isoformat(), volume, rewards, now.isoformat(), wallet)
            )
            conn.commit()
            
            # Response message
            response_text = f"""
✅ Purchase recorded for `{wallet[:6]}...{wallet[-4:]}`:
💵 Amount: {amount:,} $MN
🔥 Streak: {streak} days
💰 Streak Reward: +{reward:,} $MN
📊 Total Volume: {volume:,} $MN
🎁 Total Rewards: {rewards:,} $MN
            """.strip()
            
            bot.reply_to(message, response_text, parse_mode='Markdown')
            logger.info(f"Purchase recorded: {wallet}, amount: {amount}, streak: {streak}")
            
    except Exception as e:
        logger.error(f"Buy command error: {e}")
        bot.reply_to(message, "❌ An error occurred while recording the purchase. Please try again.")

# --- Error Handler ---
@bot.message_handler(func=lambda message: True)
def handle_unknown(message):
    """Handle unknown messages"""
    bot.reply_to(message, "❓ Unknown command. Use /help to see available commands.")

# --- Setup Webhook Function ---
def setup_webhook():
    """Setup webhook with proper error handling"""
    try:
        if RAILWAY_PUBLIC_DOMAIN:
            webhook_url = f"https://{RAILWAY_PUBLIC_DOMAIN}/{TELEGRAM_BOT_TOKEN}"
        else:
            logger.warning("RAILWAY_PUBLIC_DOMAIN not set, webhook setup may fail")
            return False
            
        # Remove existing webhook
        bot.remove_webhook()
        time.sleep(1)
        
        # Set new webhook
        bot.set_webhook(url=webhook_url)
        logger.info(f"Webhook set successfully: {webhook_url}")
        
        # Verify webhook
        webhook_info = bot.get_webhook_info()
        if webhook_info.url:
            logger.info(f"Webhook verified: {webhook_info.url}")
            return True
        else:
            logger.error("Webhook setup failed - no URL returned")
            return False
            
    except Exception as e:
        logger.error(f"Webhook setup error: {e}")
        return False

# --- Main Application ---
if __name__ == "__main__":
    try:
        logger.info("Starting MyNala Telegram Bot...")
        
        # Setup webhook
        if not setup_webhook():
            logger.error("Failed to setup webhook, exiting")
            exit(1)
        
        logger.info(f"✅ MyNala Bot is live via webhook on port {PORT}")
        logger.info(f"Health check available at: /health")
        logger.info(f"Webhook endpoint: /{TELEGRAM_BOT_TOKEN}")
        
        # Start Flask app
        app.run(host="0.0.0.0", port=PORT, debug=False)
        
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        exit(1)