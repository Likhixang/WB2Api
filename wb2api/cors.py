"""Browser access is confined to bearer-authenticated inference routes."""
from starlette.middleware.cors import CORSMiddleware


class InferenceCORSMiddleware:
    def __init__(self, app):
        self.app = app
        self.cors = CORSMiddleware(
            app,
            allow_origins=['*'],
            allow_credentials=False,
            allow_methods=['GET', 'POST'],
            allow_headers=['*'],
            expose_headers=[
                'X-Request-ID', 'Retry-After', 'X-WB2Api-Ignored-Parameters',
                'X-WB2Api-Reasoning-Mode', 'X-WB2Api-Reasoning-Effort',
                'X-WB2Api-Tool-Stream', 'X-WB2Api-Token-Count-Mode',
                'X-WB2Api-Context-Tokens', 'X-WB2Api-Max-Input-Tokens',
                'X-WB2Api-Max-Output-Tokens', 'X-WB2Api-Output-Budget',
            ],
            max_age=600,
        )

    async def __call__(self, scope, receive, send):
        target = self.cors if scope['type'] == 'http' and scope.get('path', '').startswith('/v1/') else self.app
        await target(scope, receive, send)
