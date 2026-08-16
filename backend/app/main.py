from collections.abc import Generator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session, sessionmaker

from app.api.dev import router as dev_router
from app.api.failed_jobs import router as failed_jobs_router
from app.api.gmail import router as gmail_router
from app.api.gmail_auth import login_router as gmail_login_router
from app.api.gmail_auth import router as gmail_auth_router
from app.api.health import router as health_router
from app.api.tickets import router as tickets_router
from app.config import Settings, get_settings
from app.database.database import create_session_factory, get_db
from app.integrations.gmail_runtime import RuntimeGmailProcessor
from app.observability import configure_logging, install_request_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    app_settings.validate_runtime()
    configure_logging(app_settings.log_level)
    expose_api_docs = app_settings.app_env != "production"
    application = FastAPI(
        title=app_settings.app_name,
        version="0.1.0",
        docs_url="/docs" if expose_api_docs else None,
        redoc_url="/redoc" if expose_api_docs else None,
        openapi_url="/openapi.json" if expose_api_docs else None,
    )

    # Configure CORS Middleware
    allowed_origins = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
        "https://ticket-agent-frontend-1032427393623.asia-south1.run.app",
    ]

    application.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    install_request_logging(application)
    application.state.settings = app_settings
    session_factory = create_session_factory(app_settings)
    application.state.session_factory = session_factory

    def app_session_dependency() -> Generator[Session, None, None]:
        yield from _session_dependency(session_factory)

    def app_settings_dependency() -> Settings:
        return app_settings

    application.dependency_overrides[get_db] = app_session_dependency
    application.dependency_overrides[get_settings] = app_settings_dependency
    if app_settings.groq_api_key:
        from app.tasks import enqueue_external_job

        application.state.gmail_processor = RuntimeGmailProcessor(
            app_settings,
            session_factory,
            enqueue_external_job,
        )
    application.include_router(health_router)
    application.include_router(tickets_router)
    application.include_router(failed_jobs_router)
    application.include_router(gmail_router)
    application.include_router(gmail_auth_router)
    application.include_router(gmail_login_router)
    if app_settings.app_env != "production":
        application.include_router(dev_router)
    return application


def _session_dependency(
    session_factory: sessionmaker[Session],
) -> Generator[Session, None, None]:
    with session_factory() as session:
        yield session


app = create_app()