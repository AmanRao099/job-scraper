import asyncio
import json
import os
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import AsyncGenerator

app = FastAPI()

# Allow CORS for React UI
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

process = None
log_queue = asyncio.Queue()

async def read_stream(stream):
    """Reads lines from the subprocess stream and puts them in a queue."""
    while True:
        line = await stream.readline()
        if not line:
            break
        # Put the line in the queue for SSE
        await log_queue.put(line.decode('utf-8'))
    await log_queue.put("[DONE]")

@app.post("/extract")
async def start_extraction():
    """Starts the main.py job scraper."""
    global process
    
    # Check if process is already running
    if process and process.returncode is None:
        return {"status": "error", "message": "Extraction already running"}
    
    import sys
    # Start the process in background
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-u", "main.py", # -u for unbuffered output
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=os.path.dirname(os.path.abspath(__file__))
    )
    
    # Start background task to read stdout
    asyncio.create_task(read_stream(process.stdout))
    
    return {"status": "success", "message": "Extraction started"}

async def log_generator() -> AsyncGenerator[str, None]:
    """Generates SSE formatted log messages."""
    while True:
        line = await log_queue.get()
        if line == "[DONE]":
            yield f"data: {json.dumps({'status': 'completed'})}\n\n"
            break
        if line.strip():
            yield f"data: {json.dumps({'log': line.strip()})}\n\n"

@app.get("/logs")
async def stream_logs():
    """SSE endpoint for streaming logs."""
    return StreamingResponse(log_generator(), media_type="text/event-stream")

@app.get("/jobs")
async def get_jobs():
    """Returns the currently scraped jobs."""
    jobs = []
    filepath = os.path.join(os.path.dirname(__file__), "output", "jobs.jsonl")
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        jobs.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return {"jobs": jobs}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
