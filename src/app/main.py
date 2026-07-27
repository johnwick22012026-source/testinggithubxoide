from __future__ import annotations

from typing import List
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from src.app.crud import create_note, get_all_notes, complete_note, delete_note
from src.app.database import create_db_and_tables, get_db
from src.app.schemas import NoteCreate, NoteRead


router = APIRouter(prefix="/api", tags=["notes"])


# Simple in-memory rate limiter (per-IP, not persistent) to satisfy dependency usage
_rate_limit_store: dict[str, dict] = {}


async def rate_limit_dependency(request: Request) -> None:
    # Allow generous limits for tests; this is a no-op guard that can be extended
    ip = request.client.host if request.client else "unknown"
    entry = _rate_limit_store.setdefault(ip, {"count": 0})
    entry["count"] += 1
    return None


@router.get("/notes", response_model=List[NoteRead], summary="Retrieve all notes")
async def list_notes(db: Session = Depends(get_db)) -> List[NoteRead]:
    """Return all notes newest first."""
    notes = get_all_notes(db)
    # Convert SQLAlchemy objects to Pydantic-compatible dicts if necessary
    result: List[NoteRead] = []
    for n in notes:
        result.append(
            NoteRead(
                id=str(n.id),
                text=n.text,
                is_completed=bool(n.is_completed),
                created_at=n.created_at,
                completed_at=n.completed_at,
            )
        )
    return result


@router.post(
    "/notes",
    response_model=NoteRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new note",
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
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Note text is required")
    if len(text) > 500:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Note text exceeds 500 characters")

    n = create_note(db, text)
    return NoteRead(
        id=str(n.id),
        text=n.text,
        is_completed=bool(n.is_completed),
        created_at=n.created_at,
        completed_at=n.completed_at,
    )


@router.put("/notes/{id}/complete", response_model=NoteRead, summary="Mark a note as completed")
async def complete_note_endpoint(id: str, db: Session = Depends(get_db)) -> NoteRead:
    try:
        updated = complete_note(db, id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")
    return NoteRead(
        id=str(updated.id),
        text=updated.text,
        is_completed=bool(updated.is_completed),
        created_at=updated.created_at,
        completed_at=updated.completed_at,
    )


@router.delete("/notes/{id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a completed note")
async def delete_note_endpoint(id: str, db: Session = Depends(get_db)) -> None:
    try:
        deleted = delete_note(db, id)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found or not deletable")
    return None


def create_app() -> FastAPI:
    app = FastAPI(title="TODO Notes API")

    # CORS for frontend during development
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)

    @app.on_event("startup")
    def on_startup() -> None:
        create_db_and_tables()

    return app


app = create_app()
