FROM python:3.11-slim

WORKDIR /app

# Install dependencies
# We copy requirements first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The Application Logic (script and assets) will be mounted via Compose
# This allows you to edit the script/css on the host without rebuilding the image
CMD ["python", "docs_deployer.py"]