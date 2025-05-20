import os
import sqlite3
import time
import logging
from datetime import datetime, timedelta
from telebot import TeleBot
from solana.rpc.api import Client
import threading

# === Config ===
TELEGRAM_BOT_TOKEN = "8059034423:AAF54h8vbJJiZatEDWW7Ig257Fnd8-vnWM0"
SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"
DB_FILE = "rewards.db"
LOG_FILE = "reward_bot.log"

# === Setup ===
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = TeleBot(TELEGRAM_BOT_TOKEN)
solana_client = Client(SOLANA_RPC_URL)
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cursor = conn.cursor()

cursor.execute('''
CREATE TABLE IF NOT EXISTS users (
    wallet TEXT PRIMARY KEY,
    streak_days INTEGER DEFAULT 0,
    last_purchase TEXT DEFAULT NULL,
    total_volume INTEGER DEFAULT 0,
    referral_count INTEGER DEFAULT 0,
    total_rewards INTEGER DEFAULT 0,
    referred_by TEXT DEFAULT NULL
)
''')
conn.commit()

# === Rewards ===
BUY_STREAK_REWARDS = {3: 50000, 5: 100000, 7: 200000, 14: 500000, 30: 1000000}
VOLUME_MILESTONES = {500: 250000, 1000: 500000, 2000: 1000000}
LEADERBOARD_REWARDS = {1: 2000000, 2: 1000000, 3: 500000}
REFERRAL_BONUS_CAP = 500000
LEADERBOARD_RESET_INTERVAL = 7  # Days

# === Helper Functions ===
def verify_transaction(wallet, amount):
    try:
        response = solana_client.get_confirmed_signature_for_address2(wallet, limit=100)
        for tx in response['result']:
            if tx.get('err') is None and tx.get('memo') and int(tx.get('memo')) >= amount:
                return True
    except Exception as e:
        logging.error(f"Transaction check error for {wallet}: {e}")
    return False

def update_user(wallet, volume):
    now = datetime.utcnow()
    cursor.execute("SELECT streak_days, last_purchase, total_volume, referred_by, total_rewards FROM users WHERE wallet=?", (wallet,))
    result = cursor.fetchone()

    if result is None:
        cursor.execute("INSERT INTO users (wallet, streak_days, last_purchase, total_volume) VALUES (?, ?, ?, ?)", (wallet, 1, now.strftime("%Y-%m-%d"), volume))
        conn.commit()
        return f"New user registered with 1 day buy streak and {volume} volume."

    streak_days, last_purchase, total_volume, referred_by, total_rewards = result
    last_purchase_date = datetime.strptime(last_purchase, "%Y-%m-%d")
    streak_days = streak_days + 1 if (now - last_purchase_date).days == 1 else 1
    total_volume += volume

    cursor.execute("UPDATE users SET streak_days=?, last_purchase=?, total_volume=? WHERE wallet=?", (streak_days, now.strftime("%Y-%m-%d"), total_volume, wallet))
    conn.commit()

    reward = BUY_STREAK_REWARDS.get(streak_days, 0)
    message = f"User streak updated to {streak_days} days with {total_volume} total volume."
    if reward > 0:
        cursor.execute("UPDATE users SET total_rewards = total_rewards + ? WHERE wallet = ?", (reward, wallet))
        conn.commit()
        message = f"Congrats! {wallet} hit a {streak_days} day streak and earned {reward} $MN!"
        if referred_by:
            referral_bonus = min(int(reward * 0.05), REFERRAL_BONUS_CAP)
            cursor.execute("UPDATE users SET total_rewards = total_rewards + ? WHERE wallet = ?", (referral_bonus, referred_by))
            conn.commit()
            message += f" Referrer {referred_by} earned {referral_bonus} $MN."
    return message

def reset_leaderboard():
    while True:
        time.sleep(LEADERBOARD_RESET_INTERVAL * 86400)
        cursor.execute("SELECT wallet, total_volume FROM users ORDER BY total_volume DESC LIMIT 3")
        winners = cursor.fetchall()
        for rank, (wallet, _) in enumerate(winners, 1):
            reward = LEADERBOARD_REWARDS.get(rank, 0)
            cursor.execute("UPDATE users SET total_rewards = total_rewards + ? WHERE wallet = ?", (reward, wallet))
            conn.commit()
            try:
                bot.send_message(wallet, f"🎉 Congrats! You placed #{rank} on the leaderboard and earned {reward} $MN!")
            except Exception as e:
                logging.warning(f"Unable to notify wallet {wallet}: {e}")
        cursor.execute("UPDATE users SET total_volume = 0")
        conn.commit()

# Start leaderboard reset thread
threading.Thread(target=reset_leaderboard, daemon=True).start()

# === Bot Commands ===
@bot.message_handler(commands=['register'])
def register_user(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /register <wallet> [referrer_wallet]")
            return
        wallet = parts[1]
        referred_by = parts[2] if len(parts) > 2 else None
        cursor.execute("SELECT * FROM users WHERE wallet=?", (wallet,))
        if cursor.fetchone():
            bot.reply_to(message, "This wallet is already registered.")
            return
        cursor.execute("INSERT INTO users (wallet, streak_days, last_purchase, total_volume, referred_by) VALUES (?, ?, ?, ?, ?)",
                       (wallet, 1, datetime.utcnow().strftime("%Y-%m-%d"), 0, referred_by))
        conn.commit()
        bot.reply_to(message, f"✅ {wallet} registered.")
        if referred_by:
            cursor.execute("SELECT * FROM users WHERE wallet=?", (referred_by,))
            if cursor.fetchone():
                cursor.execute("UPDATE users SET referral_count = referral_count + 1 WHERE wallet = ?", (referred_by,))
                conn.commit()
                bot.send_message(message.chat.id, f"You were referred by {referred_by}. They received a bonus!")
    except Exception as e:
        logging.error(f"Error in /register: {e}")
        bot.reply_to(message, "❌ Error while registering wallet.")

@bot.message_handler(commands=['volume'])
def check_volume(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /volume <wallet_address>")
            return
        wallet = parts[1]
        cursor.execute("SELECT total_volume, total_rewards FROM users WHERE wallet=?", (wallet,))
        result = cursor.fetchone()
        if result:
            total_volume, total_rewards = result
            bot.reply_to(message, f"Total volume for {wallet}: {total_volume} $MN\nTotal rewards: {total_rewards} $MN")
        else:
            bot.reply_to(message, "Wallet not found. Use /register first.")
    except Exception as e:
        logging.error(f"Error in /volume: {e}")
        bot.reply_to(message, "❌ Error while checking volume.")

print("✅ MyNala Bot is running...")
bot.polling()
