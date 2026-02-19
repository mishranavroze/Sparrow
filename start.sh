#!/bin/bash
# Kill any existing uvicorn processes on port 5000
for pid in $(lsof -ti :5000 2>/dev/null); do
  kill -9 "$pid" 2>/dev/null
done
# Fallback: kill by name
pkill -9 -f "uvicorn main:app" 2>/dev/null || true
sleep 1
exec python -m uvicorn main:app --host 0.0.0.0 --port 5000
