"""FastAPI integration for SmeeMe."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, Optional

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import JSONResponse
except ImportError as e:
    raise ImportError("FastAPI not installed. Install with: uv add smeeme[fastapi]") from e

from .config import SmeeConfig
from .exceptions import SmeeError
from .runner import SmeeMe

logger = logging.getLogger(__name__)


class SmeeMeFastAPI:
    """FastAPI integration manager for SmeeMe."""

    def __init__(self, config: SmeeConfig):
        self.config = config
        self.smee: Optional[SmeeMe] = None
        
    def create_lifespan(self) -> Any:
        """Create FastAPI lifespan context manager."""
        
        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
            # Startup
            logger.info("Starting SmeeMe with FastAPI")
            self.smee = SmeeMe(self.config)
            try:
                self.smee.start()
                app.state.smee = self.smee
                yield
            finally:
                # Shutdown
                logger.info("Stopping SmeeMe with FastAPI")
                if self.smee:
                    self.smee.stop()
                    
        return lifespan
    
    def add_status_routes(self, app: FastAPI, prefix: str = "/smee") -> None:
        """Add SmeeMe status and control routes."""
        
        @app.get(f"{prefix}/status")
        async def get_smee_status() -> Dict[str, Any]:
            """Get SmeeMe status."""
            if not self.smee:
                raise HTTPException(status_code=503, detail="SmeeMe not initialized")
                
            status = self.smee.get_status()
            return status.model_dump()
        
        @app.get(f"{prefix}/metrics")
        async def get_smee_metrics() -> Dict[str, Any]:
            """Get SmeeMe metrics."""
            if not self.smee:
                raise HTTPException(status_code=503, detail="SmeeMe not initialized")
                
            metrics = self.smee.get_metrics()
            return metrics.model_dump()
        
        @app.post(f"{prefix}/test")
        async def send_test_event(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            """Send test event to smee channel."""
            if not self.smee:
                raise HTTPException(status_code=503, detail="SmeeMe not initialized")
                
            try:
                success = self.smee.send_test_event(payload)
                return {"success": success}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))
        
        @app.post(f"{prefix}/restart")
        async def restart_smee() -> Dict[str, str]:
            """Restart SmeeMe client."""
            if not self.smee:
                raise HTTPException(status_code=503, detail="SmeeMe not initialized")
                
            try:
                self.smee.restart()
                return {"status": "restarted"}
            except SmeeError as e:
                raise HTTPException(status_code=500, detail=str(e))


def attach_smee(
    app: FastAPI, 
    config: SmeeConfig,
    add_routes: bool = True,
    routes_prefix: str = "/smee"
) -> SmeeMeFastAPI:
    """Attach SmeeMe to FastAPI application with lifecycle management."""
    
    integration = SmeeMeFastAPI(config)
    
    # Set up lifespan management
    if hasattr(app, 'router') and hasattr(app.router, 'lifespan_context'):
        # FastAPI 0.93+ with lifespan context manager
        app.router.lifespan_context = integration.create_lifespan()
    else:
        # Fallback for older FastAPI versions
        @app.on_event("startup")
        async def startup():
            logger.info("Starting SmeeMe with FastAPI (legacy)")
            integration.smee = SmeeMe(config)
            integration.smee.start()
            app.state.smee = integration.smee

        @app.on_event("shutdown") 
        async def shutdown():
            logger.info("Stopping SmeeMe with FastAPI (legacy)")
            if integration.smee:
                integration.smee.stop()
    
    # Add status routes if requested
    if add_routes:
        integration.add_status_routes(app, routes_prefix)
    
    return integration


def create_webhook_app(config: SmeeConfig, **kwargs) -> FastAPI:
    """Create FastAPI app with SmeeMe integration and webhook endpoint."""
    
    app = FastAPI(
        title="SmeeMe Webhook Server",
        description="Webhook server with SmeeMe tunnel integration",
        version="0.1.0",
        **kwargs
    )
    
    # Attach SmeeMe
    integration = attach_smee(app, config)
    
    @app.post("/webhook")
    async def webhook_handler(payload: Dict[str, Any]) -> JSONResponse:
        """Handle incoming webhooks."""
        logger.info(f"Received webhook: {payload}")
        
        # Process webhook payload here
        # This is where your actual webhook logic would go
        
        return JSONResponse(
            content={"status": "received", "message": "Webhook processed"},
            status_code=200
        )
    
    @app.get("/")
    async def root() -> Dict[str, str]:
        """Root endpoint."""
        return {"message": "SmeeMe Webhook Server", "status": "running"}
    
    @app.get("/health")
    async def health_check() -> Dict[str, Any]:
        """Health check endpoint."""
        smee = getattr(app.state, 'smee', None)
        if not smee:
            return {"status": "unhealthy", "reason": "SmeeMe not initialized"}
        
        status = smee.get_status()
        return {
            "status": "healthy" if status.is_healthy else "unhealthy",
            "smee_running": status.running,
            "events_processed": status.events_received
        }
    
    return app


# Example usage and factory functions
def create_memo_webhook_app(config: SmeeConfig) -> FastAPI:
    """Create FastAPI app specifically for memo webhooks."""
    
    app = create_webhook_app(config, title="Memo Webhook Server")
    
    @app.post("/webhook/memo")
    async def memo_webhook(payload: Dict[str, Any]) -> JSONResponse:
        """Handle memo-specific webhooks."""
        logger.info(f"Received memo webhook: {payload}")
        
        # Extract memo data
        memo_data = payload.get("memo", {})
        content = memo_data.get("content", "")
        
        # Process memo (this would trigger your LLM workflows if queue is enabled)
        
        return JSONResponse(
            content={"status": "processed", "memo_id": memo_data.get("name")},
            status_code=200
        )
    
    return app


def create_development_app(smee_url: str, target_port: int = 8000) -> FastAPI:
    """Create development FastAPI app with default configuration."""
    from .config import create_dev_config
    
    config = create_dev_config(
        url=smee_url,
        target=f"http://localhost:{target_port}/webhook",
        enable_queue=True
    )
    
    return create_webhook_app(config)
