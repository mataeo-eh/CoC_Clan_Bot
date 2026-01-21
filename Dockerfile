FROM python:3.13-slim

# Install Node.js and npm with proper dependencies
RUN apt-get update && \
    apt-get install -y \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Set working directory (Railpack used /app)
WORKDIR /app

# Copy requirements first (for Docker layer caching)
COPY requirements.txt .

# Create venv exactly like Railpack did
RUN python -m venv /app/.venv

# Install Python packages into venv
RUN /app/.venv/bin/pip install --no-cache-dir -r requirements.txt

# Install MCP server globally (like Railpack did)
RUN npm install -g @modelcontextprotocol/server-filesystem

# Copy all application code
COPY . .

# Run using venv's Python (Railpack's deploy command was just "python main.py"
# but it used the venv by default - we need to be explicit)
CMD ["/app/.venv/bin/python", "main.py"]