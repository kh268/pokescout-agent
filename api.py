"""Run with: uvicorn api:app --host 127.0.0.1 --port 8000 --workers 1."""
from backend.app import create_app

app = create_app()
