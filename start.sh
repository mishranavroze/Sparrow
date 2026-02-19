#!/bin/bash
cleanup_port() {
  for pid in $(lsof -ti :5000 2>/dev/null); do
    kill -9 "$pid" 2>/dev/null
  done
  pkill -9 -f "uvicorn main:app" 2>/dev/null || true
  sleep 1
}

MAX_RETRIES=3
ATTEMPT=0

while [ $ATTEMPT -lt $MAX_RETRIES ]; do
  cleanup_port
  ATTEMPT=$((ATTEMPT + 1))
  echo "Starting uvicorn (attempt $ATTEMPT/$MAX_RETRIES)..."
  python -m uvicorn main:app --host 0.0.0.0 --port 5000
  EXIT_CODE=$?
  if [ $EXIT_CODE -eq 0 ]; then
    break
  fi
  echo "Uvicorn exited with code $EXIT_CODE, retrying in 2s..."
  sleep 2
done
