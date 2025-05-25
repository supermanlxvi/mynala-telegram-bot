# Dockerfile

FROM python:3.12-slim

# Set environment
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set work directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Expose port for Flask
EXPOSE 5000

# Run your bot
CMD ["python", "MyNala_Telegram_Reward_Bot.py"]
