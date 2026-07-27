"""FastAPI application for TODO Notes
This file provides API endpoints required by the test-suite and the frontend.
Uses existing CRUD helpers from src.app.crud and database helpers from src.app.database.
"""
from __future__ import annotations

from typing import Dict, Optional
from datetime import datetime, timedelta
import asyncio

from fastapi import FastAPI, Depends, HTTPException, Request, status, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from src.app.database import get_db, create_db_and_tables
from src.app.crud import create_note, get_all_notes, complete_note, delete_note
from src.app.schemas import NoteCreate, NoteRead, NoteList


app = FastAPI(title="TODO Notes API")

# Simple in-memory rate limiter (per-IP sliding window) for demo/testing purposes
_rate_limit_lock = asyncio.Lock()
_rate_limit_store: Dict[str, Dict[str, float]] = {}
RATE_LIMIT_REQUESTS = 60
RATE_LIMIT_WINDOW_SECONDS = 60

# Simple in-memory idempotency cache for POST /api/notes keyed by Idempotency-Key header
_idempotency_lock = asyncio.Lock()
_idempotency_cache: Dict[str, dict] = {}


async def rate_limit_dependency(request: Request) -> None:
    """Basic per-client rate limit dependency. Raises 429 when exceeded."""
    client_ip = request.client.host if request.client else "unknown"
    now = asyncio.get_event_loop().time()
    async with _rate_limit_lock:
        rec = _rate_limit_store.get(client_ip)
        if not rec:
            _rate_limit_store[client_ip] = {"count": 1, "window_start": now}
            return
        window_start = rec["window_start"]
        if now - window_start > RATE_LIMIT_WINDOW_SECONDS:
            # reset window
            _rate_limit_store[client_ip] = {"count": 1, "window_start": now}
            return
        if rec["count"] >= RATE_LIMIT_REQUESTS:
            raise HTTPException(status_code=429, detail="Too Many Requests")
        rec["count"] += 1


@app.on_event("startup")
async def on_startup() -> None:
    # Ensure DB and tables exist
    create_db_and_tables()


@app.get(
    "/api/notes",
    response_model=NoteList,
    summary="Retrieve all notes",
    description="Returns all notes ordered newest first.",
)
async def list_notes(page: int = 1, page_size: int = 100, db: Session = Depends(get_db)) -> NoteList:
    """List all notes. Pagination is supported though frontend typically fetches all notes."""
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(status_code=400, detail="Invalid pagination parameters")
    notes = get_all_notes(db)
    # Expect get_all_notes to return newest-first already; if not, sort here by created_at desc
    try:
        # attempt to slice for pagination if notes is a list
        if isinstance(notes, list):
            start = (page - 1) * page_size
            end = start + page_size
            page_notes = notes[start:end]
        else:
            page_notes = notes
    except Exception:
        page_notes = notes
    return NoteList(notes=page_notes)


@app.post(
    "/api/notes",
    response_model=NoteRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new note",
    description="Create a new note. Ignores empty/whitespace-only input and enforces max length 500.",
)
async def create_note_endpoint(
    payload: NoteCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _rl: None = Depends(rate_limit_dependency),
) -> NoteRead:
    # Validate text
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Note text is required")
    if len(text) > 500:
        raise HTTPException(status_code=400, detail="Note text must be at most 500 characters")

    # Idempotency support
    idempotency_key = request.headers.get("Idempotency-Key")
    if idempotency_key:
        async with _idempotency_lock:
            cached = _idempotency_cache.get(idempotency_key)
            if cached:
                return cached  # type: ignore

    try:
        created = create_note(db, text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Store idempotency result
    if idempotency_key:
        async with _idempotency_lock:
            try:
                # Ensure it's JSON serializable via Pydantic
                if isinstance(created, dict):
                    _idempotency_cache[idempotency_key] = created
                else:
                    # FastAPI will serialize SQLAlchemy object using Pydantic model during response.
                    # To be safe, convert via NoteRead if possible
                    try:
                        nr = NoteRead.model_validate(created)  # type: ignore[attr-defined]
                        _idempotency_cache[idempotency_key] = nr.model_dump()
                    except Exception:
                        _idempotency_cache[idempotency_key] = created  # best-effort
            except Exception:
                pass

    # Background task example (no-op placeholder)
    def _noop():
        return None

    background_tasks.add_task(_noop)

    return created


@app.put(
    "/api/notes/{id}/complete",
    response_model=NoteRead,
    summary="Mark a note as completed",
    description="Mark the note complete and set completed_at timestamp.",
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
    description="Deletes a completed note after confirmation. Only completed notes may be deleted.",
)
async def delete_note_endpoint(id: str, db: Session = Depends(get_db)) -> None:
    try:
        # delete_note should raise or return False if not allowed/found
        deleted = delete_note(db, id)
        if not deleted:
            # Could be not found or not completed
            raise HTTPException(status_code=400, detail="Note cannot be deleted (not found or not completed)")
        return None
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
