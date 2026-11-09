import sys
import uvicorn

if __name__ == "__main__":
    # Keep the Windows Proactor loop so asyncio subprocess-based services remain
    # available under Uvicorn's reload supervisor.
    loop = "asyncio.windows_events:ProactorEventLoop" if sys.platform == "win32" else "auto"
    uvicorn.run(
        "backend.app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        loop=loop,
    )
