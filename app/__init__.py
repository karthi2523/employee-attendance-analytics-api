"""
App Package Initialization
Exposes core FastAPI application instance and key models/utilities at the package level.
"""
from app.main import app, client, db

__all__ = ["app", "client", "db"]
