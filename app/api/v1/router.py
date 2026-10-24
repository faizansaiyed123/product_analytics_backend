from fastapi import APIRouter

from app.api.v1 import analytics, auth, explorer, exports, ingest, objects, organizations, projects

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(organizations.router)
api_router.include_router(projects.router)
api_router.include_router(ingest.router)
api_router.include_router(explorer.router)
api_router.include_router(analytics.router)
api_router.include_router(objects.router)
api_router.include_router(exports.router)
