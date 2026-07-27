"""FastAPI application for TODO Notes\n\nProvides endpoints required by the test-suite and the frontend. Uses existing CRUD helpers from src.app.crud and DB helpers from src.app.database.\n"""\nfrom __future__ import annotations\n\nimport asyncio\nfrom datetime import datetime, timedelta\nfrom typing import Optional\n\nfrom fastapi import FastAPI, Depends, HTTPException, Request, BackgroundTasks, status\nfrom fastapi.responses import JSONResponse\nfrom pydantic import ValidationError\nfrom sqlalchemy.orm import Session\n\nfrom src.app.database import get_db, create_db_and_tables\nfrom src.app.crud import create_note, get_all_notes, complete_note, delete_note\nfrom src.app.schemas import NoteCreate, NoteRead, NoteList\n\n# Simple in-memory rate limiter (per-IP) for demo/testing purposes\n_RATE_LIMIT = 60  # requests per minute\n_rate_store: dict[str, dict] = {}\n_rate_lock = asyncio.Lock()\n\napp = FastAPI(title="TODO Notes API")\n\n\nasync def rate_limit_dependency(request: Request) -> None:\n    """Basic per-IP rate limiting dependency. Raises 429 when limit exceeded."""
    client_ip = request.client.host if request.client else "unknown"
    now = datetime.utcnow()
    async with _rate_lock:
        entry = _rate_store.get(client_ip)
        if not entry:
            _rate_store[client_ip] = {"count": 1, "window_start": now}
            return
        window_start = entry["window_start"]
        if now - window_start > timedelta(minutes=1):
            # reset
            entry["count"] = 1
            entry["window_start"] = now
            return
        if entry["count"] >= _RATE_LIMIT:
            raise HTTPException(status_code=429, detail="Too many requests")
        entry["count"] += 1


@app.on_event("startup")
async def on_startup() -> None:
    # Ensure DB and tables exist (noop if already created)
    create_db_and_tables()


@app.get(
    "/api/notes",
    response_model=NoteList,
    summary="Retrieve all notes",
    description="Returns all notes ordered newest-first (paginated).",
)
async def list_notes(page: int = 1, page_size: int = 100, db: Session = Depends(get_db)) -> NoteList:
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(status_code=400, detail="Invalid pagination parameters")
    notes = get_all_notes(db)
    # get_all_notes expected to return list-like newest-first; if not, sort by created_at
    try:
        # Paginate in-memory for tests simplicity
        start = (page - 1) * page_size
        end = start + page_size
        sliced = notes[start:end]
        return NoteList(notes=sliced)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post(
    "/api/notes",
    response_model=NoteRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new note",
    description="Create a new note. Ignores empty or whitespace-only input and enforces max length 500.",
)
async def create_note_endpoint(
    payload: NoteCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _rl: None = Depends(rate_limit_dependency),
) -> NoteRead:
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Note text is required")
    if len(text) > 500:
        raise HTTPException(status_code=400, detail="Note text exceeds 500 characters")
    try:
        created = create_note(db, text)
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # background hook example: noop for now
    return created


@app.put(
    "/api/notes/{id}/complete",
    response_model=NoteRead,
    summary="Mark a note as completed",
    description="Marks a note completed and records completion timestamp.",
)
async def complete_note_endpoint(id: str, db: Session = Depends(get_db)) -> NoteRead:
    try:
        updated = complete_note(db, id)
        if not updated:
            raise HTTPException(status_code=404, detail="Note not found")
        return updated
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete(
    "/api/notes/{id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a completed note",
    description="Deletes a note only if it is already completed. Returns 204 on success.",
)
async def delete_note_endpoint(id: str, db: Session = Depends(get_db)) -> None:
    try:
        deleted = delete_note(db, id)
        if not deleted:
            # Could be not found or not completed; surface as 400 for business rule
            raise HTTPException(status_code=400, detail="Note not found or not completed")
        return None
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
