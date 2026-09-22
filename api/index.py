"""Vercel's entry point: the same Flask app, served as a function.

Vercel's Python runtime looks for a WSGI or ASGI application called `app`, so
this file exists only to hand it one. Everything else - the read-only mode and
the snapshot it reads - is decided in webapp/server.py and storage/paths.py
from the VERCEL environment variable, which Vercel sets on every build and
every request. Nothing has to be configured in a dashboard for a deployment to
come up read-only against its own data.
"""
from webapp.server import app

__all__ = ["app"]
