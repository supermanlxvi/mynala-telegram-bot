FROM python:3.12-slim

# Create and set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Expose port
EXPOSE 5000

# Run the bot
CMD ["python", "MyNala_Telegram_Reward_Bot.py"]
