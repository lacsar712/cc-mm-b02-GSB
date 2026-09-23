import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import Boolean, DateTime, Float, String, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.rules import classify

logger = logging.getLogger("methane")


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://app:app@localhost:54391/methane"
    jwt_secret: str = "mine-methane-dev-secret"
    quarantine_ttl_minutes: int = 30


settings = Settings()
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)
USERS = {
    "gasman": {"role": "writer", "password_hash": pwd.hash("gas123456")},
    "viewer": {"role": "reader", "password_hash": pwd.hash("view123456")},
}

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)

# 旧库没有隔离相关列时补上，DDL 在 PostgreSQL 与 SQLite 上都能执行
ADDED_COLUMNS = [
    "quarantined BOOLEAN NOT NULL DEFAULT FALSE",
    "quarantine_reason VARCHAR(200)",
    "quarantined_by VARCHAR(64)",
    "quarantined_at TIMESTAMP WITH TIME ZONE",
    "quarantine_expires_at TIMESTAMP WITH TIME ZONE",
    "quarantine_permanent BOOLEAN NOT NULL DEFAULT FALSE",
]


class Base(DeclarativeBase):
    pass


class Reading(Base):
    __tablename__ = "readings"
    id: Mapped[int] = mapped_column(primary_key=True)
    site: Mapped[str] = mapped_column(String(80))
    ch4_pct: Mapped[float] = mapped_column(Float)
    level: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    quarantined: Mapped[bool] = mapped_column(Boolean, default=False)
    quarantine_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    quarantined_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    quarantine_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    quarantine_permanent: Mapped[bool] = mapped_column(Boolean, default=False)


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float


class QuarantineIn(BaseModel):
    reason: str = Field(min_length=1, max_length=200)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def iso(value: datetime | None) -> str | None:
    value = as_aware(value)
    return value.isoformat() if value is not None else None


def reading_dict(row: Reading) -> dict:
    return {
        "id": row.id,
        "site": row.site,
        "ch4_pct": row.ch4_pct,
        "level": row.level,
        "note": row.note,
        "created_by": row.created_by,
        "created_at": iso(row.created_at),
        "quarantined": row.quarantined,
        "quarantine_reason": row.quarantine_reason,
        "quarantined_by": row.quarantined_by,
        "quarantined_at": iso(row.quarantined_at),
        "quarantine_expires_at": iso(row.quarantine_expires_at),
        "quarantine_permanent": row.quarantine_permanent,
    }


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="无效令牌") from exc
    username = payload.get("sub")
    if username not in USERS:
        raise HTTPException(status_code=401, detail="无效令牌")
    return {"username": username, "role": payload.get("role")}


def require_writer(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "writer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅瓦斯检查员可执行此操作")
    return user


sockets: set[WebSocket] = set()
app = FastAPI(title="矿井瓦斯班测台")


async def broadcast(event: dict) -> None:
    dead = []
    for ws in list(sockets):
        try:
            await ws.send_json(event)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)


def expire_due_rows(now: datetime | None = None) -> list[dict]:
    """把已过恢复时限的临时隔离行转为永久隔离，返回受影响的行。"""
    db = SessionLocal()
    try:
        now = now or utcnow()
        rows = (
            db.query(Reading)
            .filter(
                Reading.quarantined.is_(True),
                Reading.quarantine_permanent.is_(False),
                Reading.quarantine_expires_at <= now,
            )
            .all()
        )
        payloads = []
        for row in rows:
            row.quarantine_permanent = True
            payloads.append(reading_dict(row))
        if rows:
            db.commit()
        return payloads
    finally:
        db.close()


_sweeper_task: asyncio.Task | None = None
SWEEP_INTERVAL_SECONDS = 3


async def sweep_loop() -> None:
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            for payload in expire_due_rows():
                await broadcast({"type": "quarantine_expired", "reading": payload})
        except Exception:
            logger.exception("隔离超时清扫失败")


@app.on_event("startup")
async def startup():
    Base.metadata.create_all(bind=engine)
    columns = {c["name"] for c in inspect(engine).get_columns("readings")}
    if "quarantined" not in columns:
        with engine.begin() as conn:
            for ddl in ADDED_COLUMNS:
                conn.execute(text(f"ALTER TABLE readings ADD COLUMN {ddl}"))
    db = SessionLocal()
    try:
        if db.query(Reading).count() == 0:
            now = utcnow()
            for site, ch4 in (("东翼-12", 0.35), ("回风巷", 1.4)):
                level, note = classify(ch4)
                db.add(
                    Reading(
                        site=site,
                        ch4_pct=ch4,
                        level=level,
                        note=note,
                        created_by="gasman",
                        created_at=now,
                    )
                )
            db.commit()
    finally:
        db.close()
    global _sweeper_task
    _sweeper_task = asyncio.create_task(sweep_loop())


@app.on_event("shutdown")
async def shutdown():
    if _sweeper_task is not None:
        _sweeper_task.cancel()


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "mine-methane-shift"}


@app.post("/api/auth/login")
def login(body: LoginIn):
    user = USERS.get(body.username.strip())
    if not user or not pwd.verify(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    token = jwt.encode(
        {"sub": body.username.strip(), "role": user["role"], "exp": exp},
        settings.jwt_secret,
        algorithm="HS256",
    )
    return {"access_token": token, "username": body.username.strip(), "role": user["role"]}


@app.get("/api/readings")
def list_readings(_user: dict = Depends(current_user)):
    """总表：隔离行默认不可见。"""
    db = SessionLocal()
    try:
        rows = (
            db.query(Reading)
            .filter(Reading.quarantined.is_(False))
            .order_by(Reading.id.desc())
            .all()
        )
        return [reading_dict(r) for r in rows]
    finally:
        db.close()


@app.get("/api/quarantine")
def list_quarantine(_user: dict = Depends(current_user)):
    """隔离区：登录账号（含旁观）都可查看。"""
    db = SessionLocal()
    try:
        now = utcnow()
        rows = (
            db.query(Reading)
            .filter(Reading.quarantined.is_(True))
            .order_by(Reading.quarantined_at.desc())
            .all()
        )
        result = []
        for r in rows:
            item = reading_dict(r)
            expires_at = as_aware(r.quarantine_expires_at)
            if r.quarantine_permanent or expires_at is None:
                item["seconds_left"] = 0
            else:
                item["seconds_left"] = max(0, int((expires_at - now).total_seconds()))
            result.append(item)
        return result
    finally:
        db.close()


@app.post("/api/readings", status_code=201)
async def create_reading(body: ReadingIn, user: dict = Depends(require_writer)):
    level, note = classify(body.ch4_pct)
    db = SessionLocal()
    try:
        row = Reading(
            site=body.site.strip(),
            ch4_pct=body.ch4_pct,
            level=level,
            note=note,
            created_by=user["username"],
            created_at=utcnow(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        payload = reading_dict(row)
    finally:
        db.close()
    await broadcast({"type": "reading", "reading": payload})
    return payload


@app.post("/api/readings/{reading_id}/quarantine")
async def quarantine_reading(reading_id: int, body: QuarantineIn, user: dict = Depends(require_writer)):
    reason = body.reason.strip()
    if not reason:
        raise HTTPException(status_code=400, detail="隔离必须填写原因")
    db = SessionLocal()
    try:
        row = db.get(Reading, reading_id)
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        if row.quarantined:
            detail = "该行已永久隔离" if row.quarantine_permanent else "该行已在隔离区"
            raise HTTPException(status_code=409, detail=detail)
        now = utcnow()
        row.quarantined = True
        row.quarantine_reason = reason
        row.quarantined_by = user["username"]
        row.quarantined_at = now
        row.quarantine_expires_at = now + timedelta(minutes=settings.quarantine_ttl_minutes)
        row.quarantine_permanent = False
        db.commit()
        payload = reading_dict(row)
    finally:
        db.close()
    await broadcast({"type": "quarantined", "reading": payload})
    return payload


@app.post("/api/readings/{reading_id}/restore")
async def restore_reading(reading_id: int, user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        row = db.get(Reading, reading_id)
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        if not row.quarantined:
            raise HTTPException(status_code=409, detail="该行不在隔离区")
        expires_at = as_aware(row.quarantine_expires_at)
        if row.quarantine_permanent or (expires_at is not None and expires_at <= utcnow()):
            # 清扫任务可能还没跑到，这里即时转永久，保证超时一定不能恢复
            row.quarantine_permanent = True
            db.commit()
            raise HTTPException(status_code=409, detail="隔离已超时，该行已永久隔离，不能恢复")
        row.quarantined = False
        row.quarantine_reason = None
        row.quarantined_by = None
        row.quarantined_at = None
        row.quarantine_expires_at = None
        row.quarantine_permanent = False
        db.commit()
        payload = reading_dict(row)
    finally:
        db.close()
    await broadcast({"type": "restored", "reading": payload})
    return payload


@app.websocket("/ws/alerts")
async def alerts(ws: WebSocket):
    await ws.accept()
    sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets.discard(ws)
