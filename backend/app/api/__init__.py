from app.api import chat, files, index, models, privacy, search, settings, system, workspace

ROUTERS = [system.router, models.router, workspace.router, files.router, index.router, search.router,
           chat.router, privacy.router, settings.router]
