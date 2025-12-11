# Dockerfile for FastAPI Chatbot Service
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Ollama
RUN curl -fsSL https://ollama.com/install.sh | sh

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Create necessary directories
RUN mkdir -p /app/docs /app/vector_store /app/vectorstore

# Expose port
EXPOSE 8002

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8002/health || exit 1

# Start script to run Ollama and FastAPI
CMD ollama serve & \
    sleep 10 && \
    ollama pull gemma2:9b && \
    uvicorn chatbot-v4:app --host 0.0.0.0 --port 8002
