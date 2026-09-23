from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import DateTime, Float, String, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.rules import classify


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://app:app@localhost:54391/methane"
    jwt_secret: str = "mine-methane-dev-secret"
    # 隔离行在该时限内可恢复回总表，超时转为永久隔离
    quarantine_ttl_seconds: int = 600


settings = Settings()
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)
USERS = {
    "gasman": {"role": "writer", "password_hash": pwd.hash("gas123456")},
    "viewer": {"role": "reader", "password_hash": pwd.hash("view123456")},
}

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


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
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    quarantine_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    quarantined_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float


class QuarantineIn(BaseModel):
    reason: str = Field(min_length=1, max_length=200)


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


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def quarantine_deadline(row: Reading) -> datetime:
    at = row.quarantined_at
    if at.tzinfo is None:
        # SQLite 等不存时区的库按 UTC 处理
        at = at.replace(tzinfo=timezone.utc)
    return at + timedelta(seconds=settings.quarantine_ttl_seconds)


def reading_dict(row: Reading) -> dict:
    return {
        "id": row.id,
        "site": row.site,
        "ch4_pct": row.ch4_pct,
        "level": row.level,
        "note": row.note,
        "created_by": row.created_by,
    }


def quarantine_dict(row: Reading, now: datetime) -> dict:
    deadline = quarantine_deadline(row)
    return {
        **reading_dict(row),
        "quarantine_reason": row.quarantine_reason,
        "quarantined_by": row.quarantined_by,
        "quarantined_at": row.quarantined_at.isoformat(),
        "restore_deadline": deadline.isoformat(),
        "permanent": now >= deadline,
    }


sockets: set[WebSocket] = set()
app = FastAPI(title="矿井瓦斯班测台")


async def broadcast(payload: dict):
    dead = []
    for ws in list(sockets):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        # 老库缺隔离列时补上，create_all 不会改已有表
        cols = {c["name"] for c in inspect(conn).get_columns("readings")}
        if "quarantined_at" not in cols:
            conn.execute(text("ALTER TABLE readings ADD COLUMN quarantined_at TIMESTAMPTZ"))
        if "quarantine_reason" not in cols:
            conn.execute(text("ALTER TABLE readings ADD COLUMN quarantine_reason VARCHAR(200)"))
        if "quarantined_by" not in cols:
            conn.execute(text("ALTER TABLE readings ADD COLUMN quarantined_by VARCHAR(64)"))
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


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "mine-methane-shift"}


@app.post("/api/auth/login")
def login(body: LoginIn):
    user = USERS.get(body.username.strip())
    if not user or not pwd.verify(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    exp = utcnow() + timedelta(hours=8)
    token = jwt.encode(
        {"sub": body.username.strip(), "role": user["role"], "exp": exp},
        settings.jwt_secret,
        algorithm="HS256",
    )
    return {"access_token": token, "username": body.username.strip(), "role": user["role"]}


@app.get("/api/readings")
def list_readings(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = (
            db.query(Reading)
            .filter(Reading.quarantined_at.is_(None))
            .order_by(Reading.id.desc())
            .all()
        )
        return [reading_dict(r) for r in rows]
    finally:
        db.close()


@app.get("/api/quarantine")
def list_quarantine(_user: dict = Depends(current_user)):
    now = utcnow()
    db = SessionLocal()
    try:
        rows = (
            db.query(Reading)
            .filter(Reading.quarantined_at.isnot(None))
            .order_by(Reading.quarantined_at.desc())
            .all()
        )
        return [quarantine_dict(r, now) for r in rows]
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
        raise HTTPException(status_code=422, detail="必须填写隔离原因")
    db = SessionLocal()
    try:
        row = db.get(Reading, reading_id)
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        if row.quarantined_at is not None:
            raise HTTPException(status_code=409, detail="该记录已在隔离区")
        row.quarantined_at = utcnow()
        row.quarantine_reason = reason
        row.quarantined_by = user["username"]
        db.commit()
    finally:
        db.close()
    await broadcast({"type": "quarantined", "id": reading_id})
    return {"id": reading_id, "quarantined": True}


@app.post("/api/readings/{reading_id}/restore")
async def restore_reading(reading_id: int, user: dict = Depends(require_writer)):
    db = SessionLocal()
    try:
        row = db.get(Reading, reading_id)
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        if row.quarantined_at is None:
            raise HTTPException(status_code=409, detail="该记录不在隔离区")
        if utcnow() >= quarantine_deadline(row):
            raise HTTPException(status_code=409, detail="已超过恢复时限，该记录已永久隔离")
        row.quarantined_at = None
        row.quarantine_reason = None
        row.quarantined_by = None
        db.commit()
        db.refresh(row)
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
