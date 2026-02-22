"""
Hysteria 2 — HTTP Auth Backend

Hysteria calls this endpoint to validate client passwords.
Config in Hysteria: auth.type = http, auth.http.url = http://127.0.0.1:8787/auth
"""
from aiohttp import web
from database import KeyDatabase


class AuthBackend:
    def __init__(self, db: KeyDatabase, port: int = 8787):
        self.db = db
        self.port = port
        self.app = web.Application()
        self.app.router.add_post("/auth", self.handle_auth)
        self.runner = None

    async def handle_auth(self, request: web.Request) -> web.Response:
        """
        Hysteria 2 sends POST with JSON body:
        { "addr": "1.2.3.4:12345", "auth": "the_password", "tx": 0, "rx": 0 }

        Must return 200 OK if valid, anything else (e.g. 403) if invalid.
        """
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="bad request")

        password = data.get("auth", "")
        if self.db.validate_key(password):
            # Return 200 with JSON body (Hysteria expects JSON on success)
            return web.json_response({"ok": True})
        else:
            return web.Response(status=403, text="unauthorized")

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", self.port)
        await site.start()

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
