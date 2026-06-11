# Use Python 3.11 slim for small image size
FROM python:3.11-slim

# Prevent Python from writing .pyc files and buffer stdout
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for Docker layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY rda_tool.py .

# Create data directories
RUN mkdir -p /app/rda_captures /app/data

# Expose the port Railway will provide
EXPOSE ${PORT:-8443}

# Use shell form so PORT env variable expands correctly
CMD python rda_tool.py