"""Convenience launcher: python run.py"""
import os

import uvicorn

if __name__ == "__main__":
    # Hosting providers assign the port and pass it in; 8000 is the local
    # default. Behind their reverse proxy the app is reached over HTTPS
    # while the proxy speaks plain HTTP to us, so the forwarded headers
    # have to be trusted or every link the app builds says "http".
    public = os.environ.get("SUCHAK_PUBLIC", "").strip().lower() in (
        "1", "true", "yes")
    uvicorn.run("app.main:app", host="0.0.0.0",
                port=int(os.environ.get("PORT", "8000")),
                proxy_headers=public,
                forwarded_allow_ips="*" if public else "127.0.0.1")
