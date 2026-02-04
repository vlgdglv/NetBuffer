
import os, json, time
import asyncio
from contextlib import asynccontextmanager
from typing import List
import aiosqlite
from fastapi import FastAPI, Request, UploadFile, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette import EventSourceResponse


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FDIR = os.path.join(BASE_DIR, 'uploads')
DB_PATH = os.path.join(BASE_DIR, "db.sqlite")
os.makedirs(UPLOAD_FDIR, exist_ok=True)

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT,
                filepath TEXT,
                size INTEGER,
                upload_time REAL,
                expires_at REAL
            )               
        """)
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS clipboard(
                id INTEGER PRIMARY KEY CHECK (id = 1),
                content TEXT,
                updated_at REAL
            )                     
        """)
        
        await db.execute("INSERT OR IGNORE INTO clipboard (id, content, updated_at) VALUES (1, '', ?)", (time.time(),))
        await db.commit()

async def cleanup_loop():
    while True:
        try:
            await asyncio.sleep(100)
            now = time.time()
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT id, filepath FROM files WHERE expires_at < ?", (now,)) as cursor:
                    expired = await cursor.fetchall()
                
                if expired:
                    print(f"[Cleanup] Found {len(expired)} expired files.")
                    for pid, filepath in expired:
                        if os.path.exists(filepath):
                            try:
                                os.remove(filepath)
                            except OSError:
                                pass
                        await db.execute("DELETE FROM files WHERE id = ?", (pid,))
                    await db.commit()
                    if manager:
                        await manager.broadcast("files_update", {"action": "cleanup"})
                        
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[Cleanup Error] {e}")
        
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    task = asyncio.create_task(cleanup_loop())
    yield

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    
app = FastAPI(lifespan=lifespan)

# === SSE Manager ===
class ConnectionManager:
    def __init__(self):
        self.activate_connections: List[asyncio.Queue] = []
        
    async def connect(self):
        queue = asyncio.Queue()
        self.activate_connections.append(queue)
        return queue
    
    def disconnect(self, queue):
        if queue in self.activate_connections:
            self.activate_connections.remove(queue)
    
    async def broadcast(self, event_type: str, data: dict):
        message = {"event": event_type, "data": json.dumps(data)}
        for queue in self.activate_connections:
            await queue.put(message)
            
manager = ConnectionManager()

@app.get("/")
async def index():
    return FileResponse(os.path.join(BASE_DIR, "static/index.html"))

@app.get("/events")
async def sse_endpoint(request: Request):
    queue = await manager.connect()
    
    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                message = await queue.get()
                yield message
        except asyncio.CancelledError:
            pass
        finally:
            manager.disconnect(queue)
            
    return EventSourceResponse(event_generator())

@app.get("/api/clipboard")
async def get_clipboard():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT content FROM clipboard WHERE id = 1") as cursor:
            row = await cursor.fetchone()
            if row is None:
                return {"content": "", "updated_at": 0}
            return {"content": row[0] if row else ""}
        
@app.post("/api/clipboard")
async def update_clipboard(data: dict):
    content = data.get("content", [])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE clipboard SET content = ?, updated_at = ? WHERE id = 1", (content, time.time()))
        await db.commit()
        
    await manager.broadcast("clipboard", {"content": content})
    return {"status": "success"}

@app.get("/api/files")
async def list_files():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM files ORDER BY upload_time DESC") as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

@app.post("/api/upload")
async def upload_file(file: UploadFile, background_tasks: BackgroundTasks):
    filename = file.filename
    save_name = f"{int(time.time())}_{filename}"
    file_path = os.path.join(UPLOAD_FDIR, save_name)
    
    size = 0
    with open(file_path, "wb") as buffer:
        while content := await file.read(1024 * 1024):
            buffer.write(content)
            size += len(content)
            
    expires_at = time.time() + (1*60*60)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO files (filename, filepath, size, upload_time, expires_at) VALUES (?, ?, ?, ?, ?)",
            (filename, file_path, size, time.time(), expires_at)
        )
        await db.commit()
        
    await manager.broadcast("files_update", {"action": "refresh"})
    return {"status": "success"}

@app.get("/api/download/{file_id}")
async def download_file(file_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT filename, filepath FROM files WHERE id = ?", (file_id,)) as cursor:
            row = await cursor.fetchone()
            if row and os.path.exists(row[1]):
                return FileResponse(row[1], filename=row[0])
            else:
                return {"status": "error, File not found"}
            
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)