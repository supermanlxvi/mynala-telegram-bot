import os
import sqlite3
import time
import logging
from datetime import datetime, timedelta
from telebot import TeleBot
from solana.rpc.api import Client
import threading
from dotenv import load_dotenv

# --- Load environment variables from .env ---
load_dotenv()

# --- Configuration ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL")
DB_FILE = "rewards.db"
LOG_FILE = "reward_bot.log"

# --- Logging Setup ---
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# --- Global Instances (initialized once) ---
bot = TeleBot(TELEGRAM_BOT_TOKEN)
solana_client = Client(SOLANA_RPC_URL)

# --- Database Setup ---
db_lock = threading.Lock()
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

# --- Leaderboard Handler ---
@bot.message_handler(commands=['leaderboard'])
def leaderboard(message):
    with db_lock:
        cursor.execute("SELECT wallet, total_volume FROM users WHERE verified = 1 ORDER BY total_volume DESC LIMIT 5")
        top = cursor.fetchall()

    if not top:
        bot.reply_to(message, "No leaderboard data available yet.")
        return

    response = "\U0001F3C6 Leaderboard:\n"
    for i, (wallet, volume) in enumerate(top, 1):
        response += f"{i}. `{wallet[:6]}...{wallet[-4:]}` - {volume} $MN\n"

    bot.reply_to(message, response, parse_mode='Markdown')

# --- Command Handlers ---
@bot.message_handler(commands=['verify'])
def verify_wallet(message):
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /verify <wallet_address>")
        return

    wallet = parts[1]
    chat_id = message.chat.id

    with db_lock:
        cursor.execute("SELECT * FROM users WHERE wallet=?", (wallet,))
        result = cursor.fetchone()
        if result:
            cursor.execute("UPDATE users SET verified=1 WHERE wallet=?", (wallet,))
        else:
            now = datetime.utcnow().strftime("%Y-%m-%d")
            cursor.execute("INSERT INTO users (chat_id, wallet, verified, last_purchase) VALUES (?, ?, ?, ?)", (chat_id, wallet, 1, now))
        conn.commit()
    bot.reply_to(message, f"✅ Wallet {wallet} is now verified.")

@bot.message_handler(commands=['status'])
def check_status(message):
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /status <wallet_address>")
        return

    wallet = parts[1]
    with db_lock:
        cursor.execute("SELECT verified, streak_days, total_volume, total_rewards FROM users WHERE wallet=?", (wallet,))
        result = cursor.fetchone()

    if result:
        verified, streak, volume, rewards = result
        status = "✅ Verified" if verified else "❌ Not Verified"
        bot.reply_to(message, f"📊 Status for `{wallet[:6]}...{wallet[-4:]}`:\n{status}\n📈 Volume: {volume} $MN\n🔥 Streak: {streak} days\n💰 Rewards: {rewards} $MN", parse_mode='Markdown')
    else:
        bot.reply_to(message, "Wallet not found. Please verify first.")

@bot.message_handler(commands=['referrals'])
def check_referrals(message):
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /referrals <wallet_address>")
        return

    wallet = parts[1]
    with db_lock:
        cursor.execute("SELECT referral_count FROM users WHERE wallet=?", (wallet,))
        result = cursor.fetchone()

    if result:
        bot.reply_to(message, f"📣 Wallet {wallet} has {result[0]} referral(s).")
    else:
        bot.reply_to(message, "Wallet not found.")

@bot.message_handler(commands=['claim'])
def claim_rewards(message):
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /claim <wallet_address>")
        return

    wallet = parts[1]
    with db_lock:
        cursor.execute("SELECT total_rewards FROM users WHERE wallet=?", (wallet,))
        result = cursor.fetchone()

    if result:
        bot.reply_to(message, f"💰 Wallet {wallet} has {result[0]} $MN in rewards.")
    else:
        bot.reply_to(message, "Wallet not found.")

@bot.message_handler(commands=['buy'])
def buy_tokens(message):
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
    now = datetime.utcnow()

    with db_lock:
        cursor.execute("SELECT streak_days, last_purchase, total_volume, total_rewards FROM users WHERE wallet=?", (wallet,))
        result = cursor.fetchone()
        if result:
            streak, last_purchase, volume, rewards = result
            if last_purchase:
                last_date = datetime.strptime(last_purchase, "%Y-%m-%d")
                if (now - last_date).days == 1:
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
            bot.reply_to(message, f"✅ Buy of {amount} $MN recorded for {wallet}.\n🔥 Streak: {streak} days\n💰 Total Rewards: {rewards} $MN")
        else:
            bot.reply_to(message, "Wallet not found. Please verify first.")

# --- Bot Start ---
if __name__ == "__main__":
    print("✅ MyNala Bot is running...")
    bot.polling(none_stop=True)
