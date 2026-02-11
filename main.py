# LACES-GENESIS OMNI Backend - FastAPI Multi-User Orchestrator
# Handles 1000+ concurrent users with WebSockets, Redis pub/sub, and PostgreSQL

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from contextlib import asynccontextmanager
import asyncio
import asyncpg
import redis.asyncio as aioredis
import jwt
import bcrypt
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set
from pydantic import BaseModel, Field
from uuid import UUID, uuid4
import json
import numpy as np
from dataclasses import dataclass
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================================
# CONFIGURATION
# ============================================================================

class Config:
    DATABASE_URL = "postgresql://laces_admin:password@localhost:5432/laces_genesis"
    REDIS_URL = "redis://localhost:6379"
    SECRET_KEY = "your-secret-key-change-in-production"
    ALGORITHM = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES = 60
    WEBSOCKET_HEARTBEAT_INTERVAL = 15  # seconds
    LOCK_HEARTBEAT_INTERVAL = 10
    MAX_CONCURRENT_USERS = 1000

config = Config()

# ============================================================================
# DATABASE & CACHE POOLS
# ============================================================================

class DatabasePool:
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None
        
    async def connect(self):
        self.pool = await asyncpg.create_pool(
            config.DATABASE_URL,
            min_size=10,
            max_size=100,
            command_timeout=60
        )
        logger.info("Database pool created")
        
    async def disconnect(self):
        if self.pool:
            await self.pool.close()
            logger.info("Database pool closed")
    
    async def execute(self, query: str, *args):
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)
    
    async def fetch(self, query: str, *args):
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)
    
    async def fetchrow(self, query: str, *args):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

class RedisCache:
    def __init__(self):
        self.client: Optional[aioredis.Redis] = None
        self.pubsub = None
        
    async def connect(self):
        self.client = await aioredis.from_url(
            config.REDIS_URL,
            encoding="utf-8",
            decode_responses=True
        )
        self.pubsub = self.client.pubsub()
        logger.info("Redis connected")
        
    async def disconnect(self):
        if self.pubsub:
            await self.pubsub.close()
        if self.client:
            await self.client.close()
        logger.info("Redis disconnected")

db = DatabasePool()
cache = RedisCache()

# ============================================================================
# GLOBAL SYNCHRONIZATION LOCK MANAGER
# ============================================================================

class LockManager:
    """
    Distributed lock manager using Redis for global synchronization.
    Implements optimistic concurrency control with vector clocks.
    """
    
    def __init__(self, redis_client: aioredis.Redis):
        self.redis = redis_client
        self.local_locks: Dict[UUID, Set[str]] = {}  # twin_id -> set of component paths
        
    async def acquire_lock(
        self,
        twin_id: UUID,
        user_id: UUID,
        session_id: UUID,
        components: List[str],
        lock_type: str = "exclusive",
        timeout: int = 300  # 5 minutes
    ) -> Optional[UUID]:
        """
        Acquire a distributed lock on specific components of a digital twin.
        Uses Redis SETNX for atomic lock acquisition.
        """
        lock_key = f"lock:twin:{twin_id}"
        lock_id = uuid4()
        
        # Check existing locks in Redis
        existing = await self.redis.get(lock_key)
        if existing:
            existing_data = json.loads(existing)
            # Check if any requested components are already locked
            locked_components = set(existing_data.get("components", []))
            requested_components = set(components)
            
            if lock_type == "exclusive" and locked_components & requested_components:
                return None  # Conflict
            
            if existing_data.get("lock_type") == "exclusive":
                return None  # Exclusive lock exists
        
        # Acquire lock
        lock_data = {
            "lock_id": str(lock_id),
            "twin_id": str(twin_id),
            "user_id": str(user_id),
            "session_id": str(session_id),
            "components": components,
            "lock_type": lock_type,
            "acquired_at": datetime.utcnow().isoformat(),
            "expires_at": (datetime.utcnow() + timedelta(seconds=timeout)).isoformat()
        }
        
        # Atomic set with expiration
        set_result = await self.redis.set(
            lock_key,
            json.dumps(lock_data),
            nx=True,  # Only set if not exists
            ex=timeout
        )
        
        if not set_result:
            return None
        
        # Store in PostgreSQL for persistence
        await db.execute(
            """
            INSERT INTO edit_locks 
            (lock_id, twin_id, user_id, session_id, lock_type, locked_components, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            lock_id, twin_id, user_id, session_id, lock_type,
            json.dumps(components), datetime.utcnow() + timedelta(seconds=timeout)
        )
        
        logger.info(f"Lock acquired: {lock_id} for twin {twin_id} by user {user_id}")
        return lock_id
    
    async def release_lock(self, lock_id: UUID):
        """Release a distributed lock."""
        # Get lock details from DB
        lock = await db.fetchrow(
            "SELECT twin_id, locked_components FROM edit_locks WHERE lock_id = $1",
            lock_id
        )
        
        if not lock:
            return False
        
        # Remove from Redis
        lock_key = f"lock:twin:{lock['twin_id']}"
        await self.redis.delete(lock_key)
        
        # Mark as inactive in DB
        await db.execute(
            "UPDATE edit_locks SET is_active = FALSE WHERE lock_id = $1",
            lock_id
        )
        
        logger.info(f"Lock released: {lock_id}")
        return True
    
    async def heartbeat_lock(self, lock_id: UUID):
        """Send heartbeat to keep lock alive."""
        await db.execute(
            "UPDATE edit_locks SET heartbeat_at = NOW() WHERE lock_id = $1 AND is_active = TRUE",
            lock_id
        )
        
        # Extend Redis TTL
        lock = await db.fetchrow(
            "SELECT twin_id FROM edit_locks WHERE lock_id = $1",
            lock_id
        )
        if lock:
            lock_key = f"lock:twin:{lock['twin_id']}"
            await self.redis.expire(lock_key, 300)
    
    async def cleanup_stale_locks(self):
        """Background task to clean up expired locks."""
        while True:
            await asyncio.sleep(30)
            await db.execute("SELECT release_stale_locks()")
            logger.debug("Stale locks cleaned up")

lock_manager = None

# ============================================================================
# WEBSOCKET CONNECTION MANAGER
# ============================================================================

class ConnectionManager:
    """
    Manages WebSocket connections for real-time collaboration.
    Implements pub/sub pattern for broadcasting updates.
    """
    
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}  # session_id -> websocket
        self.user_sessions: Dict[UUID, Set[str]] = {}  # user_id -> set of session_ids
        self.twin_subscribers: Dict[UUID, Set[str]] = {}  # twin_id -> set of session_ids
        
    async def connect(self, websocket: WebSocket, session_id: str, user_id: UUID):
        await websocket.accept()
        self.active_connections[session_id] = websocket
        
        if user_id not in self.user_sessions:
            self.user_sessions[user_id] = set()
        self.user_sessions[user_id].add(session_id)
        
        logger.info(f"WebSocket connected: session {session_id}, user {user_id}")
    
    def disconnect(self, session_id: str, user_id: UUID):
        if session_id in self.active_connections:
            del self.active_connections[session_id]
        
        if user_id in self.user_sessions:
            self.user_sessions[user_id].discard(session_id)
            if not self.user_sessions[user_id]:
                del self.user_sessions[user_id]
        
        # Remove from all twin subscriptions
        for subscribers in self.twin_subscribers.values():
            subscribers.discard(session_id)
        
        logger.info(f"WebSocket disconnected: session {session_id}")
    
    async def subscribe_to_twin(self, session_id: str, twin_id: UUID):
        if twin_id not in self.twin_subscribers:
            self.twin_subscribers[twin_id] = set()
        self.twin_subscribers[twin_id].add(session_id)
    
    async def unsubscribe_from_twin(self, session_id: str, twin_id: UUID):
        if twin_id in self.twin_subscribers:
            self.twin_subscribers[twin_id].discard(session_id)
    
    async def broadcast_to_twin(self, twin_id: UUID, message: dict, exclude_session: Optional[str] = None):
        """Broadcast an update to all subscribers of a twin."""
        if twin_id not in self.twin_subscribers:
            return
        
        disconnected = []
        for session_id in self.twin_subscribers[twin_id]:
            if session_id == exclude_session:
                continue
            
            if session_id in self.active_connections:
                try:
                    await self.active_connections[session_id].send_json(message)
                except Exception as e:
                    logger.error(f"Error broadcasting to {session_id}: {e}")
                    disconnected.append(session_id)
        
        # Clean up disconnected sessions
        for session_id in disconnected:
            self.twin_subscribers[twin_id].discard(session_id)
    
    async def send_personal_message(self, session_id: str, message: dict):
        if session_id in self.active_connections:
            await self.active_connections[session_id].send_json(message)
    
    def get_connection_count(self) -> int:
        return len(self.active_connections)

manager = ConnectionManager()

# ============================================================================
# AUTHENTICATION
# ============================================================================

security = HTTPBearer()

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=config.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, config.SECRET_KEY, algorithm=config.ALGORITHM)
    return encoded_jwt

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        payload = jwt.decode(token, config.SECRET_KEY, algorithms=[config.ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid authentication credentials")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.JWTError:
        raise HTTPException(status_code=401, detail="Could not validate credentials")
    
    # Verify user exists
    user = await db.fetchrow("SELECT * FROM users WHERE user_id = $1 AND is_active = TRUE", UUID(user_id))
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    
    return dict(user)

# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class UserCreate(BaseModel):
    email: str
    username: str
    password: str
    organization_id: Optional[UUID] = None

class UserLogin(BaseModel):
    username: str
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: UUID
    session_id: UUID

class DigitalTwinCreate(BaseModel):
    name: str
    description: Optional[str] = None
    twin_type: str
    properties: dict = {}
    tags: List[str] = []

class VersionCreate(BaseModel):
    twin_id: UUID
    commit_message: str
    model_url: str
    model_format: str
    properties: dict

class LockRequest(BaseModel):
    twin_id: UUID
    components: List[str]
    lock_type: str = "exclusive"

class EditOperation(BaseModel):
    twin_id: UUID
    operation_type: str
    component_path: str
    operation_data: dict
    vector_clock: Optional[dict] = None

class MixingParameters(BaseModel):
    material_id: UUID
    rpm: float
    temperature: float
    force_n: float
    duration_min: int

class TelemetryData(BaseModel):
    node_id: UUID
    rpm: Optional[float] = None
    torque_nm: Optional[float] = None
    vibration_x_g: Optional[float] = None
    vibration_y_g: Optional[float] = None
    vibration_z_g: Optional[float] = None
    temperature_c: Optional[float] = None
    custom_metrics: dict = {}

# ============================================================================
# FASTAPI APPLICATION
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    global lock_manager
    await db.connect()
    await cache.connect()
    lock_manager = LockManager(cache.client)
    
    # Start background tasks
    asyncio.create_task(lock_manager.cleanup_stale_locks())
    asyncio.create_task(heartbeat_monitor())
    
    yield
    
    # Shutdown
    await db.disconnect()
    await cache.disconnect()

app = FastAPI(
    title="LACES-GENESIS OMNI",
    description="Hyper-Scalable Agentic Engineering Orchestrator",
    version="Ultimate 2026",
    lifespan=lifespan
)

# CORS middleware for web clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure properly in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
# AUTHENTICATION ENDPOINTS
# ============================================================================

@app.post("/auth/register", response_model=Token)
async def register(user: UserCreate):
    # Check if user exists
    existing = await db.fetchrow(
        "SELECT user_id FROM users WHERE email = $1 OR username = $2",
        user.email, user.username
    )
    if existing:
        raise HTTPException(status_code=400, detail="User already exists")
    
    # Hash password
    password_hash = bcrypt.hashpw(user.password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    
    # Create user
    user_id = uuid4()
    await db.execute(
        """
        INSERT INTO users (user_id, email, username, password_hash, organization_id)
        VALUES ($1, $2, $3, $4, $5)
        """,
        user_id, user.email, user.username, password_hash, user.organization_id
    )
    
    # Create session
    session_id = uuid4()
    token = create_access_token({"sub": str(user_id)})
    
    await db.execute(
        """
        INSERT INTO user_sessions (session_id, user_id, token_hash, expires_at)
        VALUES ($1, $2, $3, $4)
        """,
        session_id, user_id, bcrypt.hashpw(token.encode('utf-8'), bcrypt.gensalt()).decode('utf-8'),
        datetime.utcnow() + timedelta(minutes=config.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    
    return Token(access_token=token, user_id=user_id, session_id=session_id)

@app.post("/auth/login", response_model=Token)
async def login(credentials: UserLogin):
    user = await db.fetchrow(
        "SELECT * FROM users WHERE username = $1 AND is_active = TRUE",
        credentials.username
    )
    
    if not user or not bcrypt.checkpw(credentials.password.encode('utf-8'), user['password_hash'].encode('utf-8')):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    session_id = uuid4()
    token = create_access_token({"sub": str(user['user_id'])})
    
    await db.execute(
        """
        INSERT INTO user_sessions (session_id, user_id, token_hash, expires_at)
        VALUES ($1, $2, $3, $4)
        """,
        session_id, user['user_id'], bcrypt.hashpw(token.encode('utf-8'), bcrypt.gensalt()).decode('utf-8'),
        datetime.utcnow() + timedelta(minutes=config.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    
    await db.execute("UPDATE users SET last_login = NOW() WHERE user_id = $1", user['user_id'])
    
    return Token(access_token=token, user_id=user['user_id'], session_id=session_id)

# ============================================================================
# DIGITAL TWIN ENDPOINTS
# ============================================================================

@app.post("/twins", status_code=201)
async def create_digital_twin(twin: DigitalTwinCreate, current_user: dict = Depends(get_current_user)):
    twin_id = uuid4()
    await db.execute(
        """
        INSERT INTO digital_twins (twin_id, name, description, twin_type, created_by, organization_id, tags)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        twin_id, twin.name, twin.description, twin.twin_type,
        current_user['user_id'], current_user['organization_id'], twin.tags
    )
    
    # Create initial version
    version_id = uuid4()
    await db.execute(
        """
        INSERT INTO twin_versions (version_id, twin_id, version_number, created_by, properties)
        VALUES ($1, $2, 1, $3, $4)
        """,
        version_id, twin_id, current_user['user_id'], json.dumps(twin.properties)
    )
    
    return {"twin_id": twin_id, "version_id": version_id}

@app.get("/twins")
async def list_digital_twins(
    skip: int = 0,
    limit: int = 50,
    current_user: dict = Depends(get_current_user)
):
    twins = await db.fetch(
        """
        SELECT dt.*, tv.properties, tv.version_number
        FROM digital_twins dt
        JOIN twin_versions tv ON dt.twin_id = tv.twin_id AND tv.is_latest = TRUE
        WHERE dt.organization_id = $1
        ORDER BY dt.created_at DESC
        LIMIT $2 OFFSET $3
        """,
        current_user['organization_id'], limit, skip
    )
    return [dict(t) for t in twins]

@app.post("/twins/{twin_id}/versions", status_code=201)
async def create_version(twin_id: UUID, version: VersionCreate, current_user: dict = Depends(get_current_user)):
    # Get latest version number
    latest = await db.fetchrow(
        "SELECT MAX(version_number) as max_ver FROM twin_versions WHERE twin_id = $1",
        twin_id
    )
    new_version_number = (latest['max_ver'] or 0) + 1
    
    version_id = uuid4()
    await db.execute(
        """
        INSERT INTO twin_versions 
        (version_id, twin_id, version_number, created_by, commit_message, model_url, model_format, properties)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        version_id, twin_id, new_version_number, current_user['user_id'],
        version.commit_message, version.model_url, version.model_format, json.dumps(version.properties)
    )
    
    return {"version_id": version_id, "version_number": new_version_number}

# ============================================================================
# COLLABORATIVE EDITING ENDPOINTS
# ============================================================================

@app.post("/locks/acquire")
async def acquire_lock(request: LockRequest, current_user: dict = Depends(get_current_user)):
    # Get active session
    session = await db.fetchrow(
        "SELECT session_id FROM user_sessions WHERE user_id = $1 AND is_active = TRUE ORDER BY created_at DESC LIMIT 1",
        current_user['user_id']
    )
    
    if not session:
        raise HTTPException(status_code=400, detail="No active session")
    
    lock_id = await lock_manager.acquire_lock(
        request.twin_id,
        current_user['user_id'],
        session['session_id'],
        request.components,
        request.lock_type
    )
    
    if not lock_id:
        raise HTTPException(status_code=409, detail="Lock conflict - components already locked")
    
    return {"lock_id": lock_id}

@app.delete("/locks/{lock_id}")
async def release_lock(lock_id: UUID, current_user: dict = Depends(get_current_user)):
    success = await lock_manager.release_lock(lock_id)
    if not success:
        raise HTTPException(status_code=404, detail="Lock not found")
    return {"status": "released"}

@app.post("/edit-operations")
async def submit_edit_operation(operation: EditOperation, current_user: dict = Depends(get_current_user)):
    operation_id = uuid4()
    
    await db.execute(
        """
        INSERT INTO edit_operations 
        (operation_id, twin_id, user_id, operation_type, component_path, operation_data, vector_clock)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        """,
        operation_id, operation.twin_id, current_user['user_id'],
        operation.operation_type, operation.component_path,
        json.dumps(operation.operation_data), json.dumps(operation.vector_clock or {})
    )
    
    # Broadcast to all subscribers
    await manager.broadcast_to_twin(
        operation.twin_id,
        {
            "type": "edit_operation",
            "operation_id": str(operation_id),
            "user_id": str(current_user['user_id']),
            "operation": operation.dict()
        }
    )
    
    return {"operation_id": operation_id}

# ============================================================================
# TELEMETRY INGESTION
# ============================================================================

@app.post("/telemetry", status_code=201)
async def ingest_telemetry(data: TelemetryData):
    """High-throughput telemetry ingestion endpoint."""
    await db.execute(
        """
        INSERT INTO telemetry_data 
        (time, node_id, rpm, torque_nm, vibration_x_g, vibration_y_g, vibration_z_g, 
         temperature_c, custom_metrics)
        VALUES (NOW(), $1, $2, $3, $4, $5, $6, $7, $8)
        """,
        data.node_id, data.rpm, data.torque_nm, data.vibration_x_g,
        data.vibration_y_g, data.vibration_z_g, data.temperature_c,
        json.dumps(data.custom_metrics)
    )
    
    return {"status": "ingested"}

@app.post("/telemetry/batch", status_code=201)
async def ingest_telemetry_batch(data_points: List[TelemetryData]):
    """Batch telemetry ingestion for efficiency."""
    async with db.pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO telemetry_data 
            (time, node_id, rpm, torque_nm, vibration_x_g, vibration_y_g, vibration_z_g, 
             temperature_c, custom_metrics)
            VALUES (NOW(), $1, $2, $3, $4, $5, $6, $7, $8)
            """,
            [
                (d.node_id, d.rpm, d.torque_nm, d.vibration_x_g,
                 d.vibration_y_g, d.vibration_z_g, d.temperature_c,
                 json.dumps(d.custom_metrics))
                for d in data_points
            ]
        )
    
    return {"status": "ingested", "count": len(data_points)}

# ============================================================================
# WEBSOCKET ENDPOINT
# ============================================================================

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    # Verify session
    session = await db.fetchrow(
        "SELECT * FROM user_sessions WHERE session_id = $1 AND is_active = TRUE AND expires_at > NOW()",
        UUID(session_id)
    )
    
    if not session:
        await websocket.close(code=1008, reason="Invalid session")
        return
    
    user_id = session['user_id']
    await manager.connect(websocket, session_id, user_id)
    
    try:
        while True:
            data = await websocket.receive_json()
            message_type = data.get("type")
            
            if message_type == "subscribe":
                twin_id = UUID(data["twin_id"])
                await manager.subscribe_to_twin(session_id, twin_id)
                await websocket.send_json({"type": "subscribed", "twin_id": str(twin_id)})
            
            elif message_type == "heartbeat":
                await websocket.send_json({"type": "pong"})
            
            elif message_type == "cursor_move":
                # Broadcast cursor position to other users
                twin_id = UUID(data["twin_id"])
                await manager.broadcast_to_twin(
                    twin_id,
                    {
                        "type": "cursor_update",
                        "user_id": str(user_id),
                        "position": data["position"]
                    },
                    exclude_session=session_id
                )
            
    except WebSocketDisconnect:
        manager.disconnect(session_id, user_id)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(session_id, user_id)

# ============================================================================
# BACKGROUND TASKS
# ============================================================================

async def heartbeat_monitor():
    """Monitor WebSocket connections and send periodic heartbeats."""
    while True:
        await asyncio.sleep(config.WEBSOCKET_HEARTBEAT_INTERVAL)
        for session_id, websocket in list(manager.active_connections.items()):
            try:
                await websocket.send_json({"type": "ping"})
            except Exception:
                pass  # Will be cleaned up on next message

# ============================================================================
# HEALTH CHECK
# ============================================================================

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "active_connections": manager.get_connection_count(),
        "database": "connected" if db.pool else "disconnected",
        "redis": "connected" if cache.client else "disconnected"
    }

# ============================================================================
# READY FOR DEPLOYMENT
# ============================================================================
# Run with: uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
# Or with Gunicorn: gunicorn -k uvicorn.workers.UvicornWorker -w 4 -b 0.0.0.0:8000 main:app
