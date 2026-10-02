import os
import shutil
import io
import re
import json
import time
import base64
import secrets
import hashlib
import urllib.request
from typing import List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Form, UploadFile, File, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse
from pypdf import PdfReader, PdfWriter

app = FastAPI(title="Ders Notları & PDF Portalı")

import asyncio

# --- Persistent Data Directory Configuration ---
DATA_DIR = os.getenv("DATA_DIR")
if not DATA_DIR:
    if os.path.exists("/data") and os.access("/data", os.W_OK):
        DATA_DIR = "/data"
    elif os.path.exists("/var/data") and os.access("/var/data", os.W_OK):
        DATA_DIR = "/var/data"
    else:
        DATA_DIR = "."

os.makedirs(DATA_DIR, exist_ok=True)
CHAT_MEDIA_DIR = os.path.join(DATA_DIR, "chat_media")
os.makedirs(CHAT_MEDIA_DIR, exist_ok=True)
app.mount("/chat_media", StaticFiles(directory=CHAT_MEDIA_DIR), name="chat_media")

BASE_UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
AUTH_FILE = os.path.join(DATA_DIR, "users.json")
COURSES_FILE = os.path.join(DATA_DIR, "courses.json")
CHAT_FILE = os.path.join(DATA_DIR, "chat_messages.json")
RATINGS_FILE = os.path.join(DATA_DIR, "ratings.json")
COMMENTS_FILE = os.path.join(DATA_DIR, "comments.json")
SESSION_TIMEOUT_SECONDS = 24 * 3600  # 24 hours
os.makedirs(BASE_UPLOAD_DIR, exist_ok=True)

# Migrate starter files to DATA_DIR if on a persistent disk and not present
if DATA_DIR != ".":
    for sf in ["courses.json", "users.json", "chat_messages.json"]:
        src = sf
        dst = os.path.join(DATA_DIR, sf)
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                shutil.copyfile(src, dst)
            except Exception:
                pass

DEFAULT_COURSES = ["Kadın Doğum Hemşireliği", "İç Hastalıkları", "Cerrahi Hastalıkları", "Sağlık Tanılaması", "Genel"]

def init_courses():
    if not os.path.exists(COURSES_FILE):
        with open(COURSES_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_COURSES, f, ensure_ascii=False, indent=2)
        for c in DEFAULT_COURSES:
            os.makedirs(os.path.join(BASE_UPLOAD_DIR, c), exist_ok=True)

init_courses()

def get_all_courses() -> List[str]:
    init_courses()
    try:
        with open(COURSES_FILE, "r", encoding="utf-8") as f:
            courses = json.load(f)
            if isinstance(courses, list):
                res = []
                seen = set()
                for c in courses:
                    s = str(c).strip()
                    if s and s not in seen:
                        seen.add(s)
                        res.append(s)
                return res
    except Exception as e:
        print(f"Error loading courses: {e}")
    return DEFAULT_COURSES.copy()

def add_course_to_store(name: str) -> None:
    courses = get_all_courses()
    clean = name.strip()
    if clean and clean not in courses:
        courses.append(clean)
        with open(COURSES_FILE, "w", encoding="utf-8") as f:
            json.dump(courses, f, ensure_ascii=False, indent=2)
    os.makedirs(os.path.join(BASE_UPLOAD_DIR, clean), exist_ok=True)

def remove_course_from_store(name: str) -> bool:
    clean = name.strip()
    courses = get_all_courses()
    if clean in courses:
        courses = [c for c in courses if c != clean]
        with open(COURSES_FILE, "w", encoding="utf-8") as f:
            json.dump(courses, f, ensure_ascii=False, indent=2)

        c_path = os.path.join(BASE_UPLOAD_DIR, clean)
        if os.path.exists(c_path):
            try:
                shutil.rmtree(c_path, ignore_errors=True)
            except Exception as e:
                print(f"Error removing course dir: {e}")
        return True
    return False

# --- Authentication Helpers ---
def hash_password(password: str, salt: str = None) -> tuple:
    if not salt:
        salt = secrets.token_hex(16)
    hashed = hashlib.sha256((password + salt).encode('utf-8')).hexdigest()
    return hashed, salt

def load_auth_data():
    if not os.path.exists(AUTH_FILE):
        admin_hash, admin_salt = hash_password("admin123")
        data = {
            "users": {
                "admin": {
                    "username": "admin",
                    "password_hash": admin_hash,
                    "salt": admin_salt,
                    "role": "admin"
                }
            },
            "sessions": {}
        }
        with open(AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return data
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"users": {}, "sessions": {}}

def save_auth_data(data):
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def get_current_user(request: Request) -> Optional[dict]:
    token = None
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
    if not token:
        token = request.cookies.get("session_token")

    if not token:
        return None

    data = load_auth_data()
    session_info = data.get("sessions", {}).get(token)
    if not session_info:
        return None

    if isinstance(session_info, str):
        username = session_info
        last_active = time.time()
        data["sessions"][token] = {"username": username, "last_active": last_active}
        save_auth_data(data)
    else:
        username = session_info.get("username")
        last_active = session_info.get("last_active", 0)

    now = time.time()
    # 24-hour inactivity check
    if now - last_active > SESSION_TIMEOUT_SECONDS:
        del data["sessions"][token]
        save_auth_data(data)
        return None

    # Sliding expiration: renew last_active
    data["sessions"][token]["last_active"] = now
    save_auth_data(data)

    if username and username in data.get("users", {}):
        return data["users"][username]
    return None

def parse_range(range_str: str, max_pages: int) -> List[int]:
    pages = set()
    parts = re.split(r'[,;\s]+', range_str.strip())
    for part in parts:
        if not part:
            continue
        if '-' in part:
            sub = part.split('-')
            if len(sub) != 2:
                continue
            start, end = int(sub[0]), int(sub[1])
            start = max(1, start)
            end = min(max_pages, end)
            for p in range(start, end + 1):
                pages.add(p - 1)
        else:
            p = int(part)
            if 1 <= p <= max_pages:
                pages.add(p - 1)
    return sorted(list(pages))

# --- Public & Config Routes ---
@app.get("/", response_class=HTMLResponse)
async def home():
    template_path = os.path.join("templates", "index.html")
    if not os.path.exists(template_path):
        template_path = "index.html"
    with open(template_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/api/config")
async def get_config():
    client_id = os.getenv("GOOGLE_CLIENT_ID", "100935865119-qaqqc9tprpt32srs7niab6kcdclks2up.apps.googleusercontent.com")
    return {"google_client_id": client_id}

@app.get("/api/auth/me")
async def auth_me(request: Request):
    user = get_current_user(request)
    if user:
        return {
            "authenticated": True,
            "username": user["username"],
            "role": user.get("role", "user")
        }
    return {"authenticated": False, "username": None, "role": "guest"}

@app.post("/api/auth/register")
async def register(response: Response, username: str = Form(...), password: str = Form(...)):
    username = username.strip().lower()
    if not username or not password or len(password) < 4:
        raise HTTPException(status_code=400, detail="Kullanıcı adı ve en az 4 karakterli şifre girin.")
    
    data = load_auth_data()
    if username in data["users"]:
        raise HTTPException(status_code=400, detail="Bu kullanıcı adı zaten alınmış.")
    
    h, s = hash_password(password)
    data["users"][username] = {
        "username": username,
        "password_hash": h,
        "salt": s,
        "role": "user"
    }
    
    token = secrets.token_hex(24)
    data["sessions"][token] = {"username": username, "last_active": time.time()}
    save_auth_data(data)
    
    response.set_cookie(key="session_token", value=token, httponly=True, max_age=SESSION_TIMEOUT_SECONDS, samesite="lax")
    return {"status": "success", "username": username, "role": "user", "token": token}

@app.post("/api/auth/login")
async def login(response: Response, username: str = Form(...), password: str = Form(...)):
    username = username.strip().lower()
    data = load_auth_data()
    user = data.get("users", {}).get(username)
    if not user:
        raise HTTPException(status_code=401, detail="Geçersiz kullanıcı adı veya şifre.")
    
    expected_hash, _ = hash_password(password, user["salt"])
    if expected_hash != user["password_hash"]:
        raise HTTPException(status_code=401, detail="Geçersiz kullanıcı adı veya şifre.")
    
    token = secrets.token_hex(24)
    data["sessions"][token] = {"username": username, "last_active": time.time()}
    save_auth_data(data)
    
    response.set_cookie(key="session_token", value=token, httponly=True, max_age=SESSION_TIMEOUT_SECONDS, samesite="lax")
    return {"status": "success", "username": username, "role": user.get("role", "user"), "token": token}

@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get("session_token")
    if not token:
        auth_header = request.headers.get("authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
    if token:
        data = load_auth_data()
        if token in data.get("sessions", {}):
            del data["sessions"][token]
            save_auth_data(data)
    response.delete_cookie(key="session_token")
    return {"status": "success"}

@app.post("/api/auth/google")
async def google_login(response: Response, credential: str = Form(...)):
    try:
        parts = credential.split(".")
        if len(parts) != 3:
            raise ValueError("Geçersiz token formatı")
        
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload_json = base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8")
        payload = json.loads(payload_json)
        
        email = payload.get("email", "").lower()
        if not email:
            raise HTTPException(status_code=400, detail="Google e-posta bilgisi alınamadı.")
            
        data = load_auth_data()
        username = email.split("@")[0]
        
        if username not in data["users"]:
            data["users"][username] = {
                "username": username,
                "email": email,
                "role": "user",
                "auth_provider": "google"
            }
            
        token = secrets.token_hex(24)
        data["sessions"][token] = {"username": username, "last_active": time.time()}
        save_auth_data(data)
        
        response.set_cookie(key="session_token", value=token, httponly=True, max_age=SESSION_TIMEOUT_SECONDS, samesite="lax")
        return {"status": "success", "username": username, "role": data["users"][username].get("role", "user"), "token": token}
    except Exception as e:
        raise HTTPException(status_code=400, detail="Google girişi doğrulanamadı: " + str(e))

# --- File Operations ---
@app.get("/api/files")
async def list_files(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Lütfen önce giriş yapın.")

    data = []
    courses = get_all_courses()
    
    for course in courses:
        c_path = os.path.join(BASE_UPLOAD_DIR, course)
        if not os.path.exists(c_path):
            continue
        for fname in os.listdir(c_path):
            if fname.lower().endswith(".pdf"):
                fpath = os.path.join(c_path, fname)
                try:
                    size = os.path.getsize(fpath)
                    reader = PdfReader(fpath)
                    page_count = len(reader.pages)
                except Exception:
                    size = 0
                    page_count = 1
                data.append({
                    "course": course,
                    "filename": fname,
                    "title": os.path.splitext(fname)[0].replace("_", " "),
                    "size": size,
                    "page_count": page_count
                })
    return {"courses": courses, "files": data}


# --- Chat & Real-Time Voice Signaling ---
CHAT_FILE = "chat_messages.json"

def load_chat_messages():
    if not os.path.exists(CHAT_FILE):
        return []
    try:
        with open(CHAT_FILE, "r", encoding="utf-8") as f:
            msgs = json.load(f)
            # Ensure seen_by field exists on all messages
            for m in msgs:
                if "seen_by" not in m:
                    m["seen_by"] = [{
                        "username": m.get("username", "user"),
                        "role": m.get("role", "user"),
                        "time": m.get("time", "")
                    }]
            return msgs
    except Exception:
        return []

def mark_messages_seen(username: str, role: str, message_ids: Optional[List[str]] = None) -> List[dict]:
    msgs = load_chat_messages()
    updated = []
    current_time = time.strftime("%H:%M")
    changed = False

    for m in msgs:
        if message_ids is not None and m.get("id") not in message_ids:
            continue
        seen_by = m.setdefault("seen_by", [])
        if not any(u.get("username") == username for u in seen_by):
            seen_by.append({
                "username": username,
                "role": role,
                "time": current_time
            })
            changed = True
            updated.append({"id": m["id"], "seen_by": seen_by})

    if changed:
        try:
            with open(CHAT_FILE, "w", encoding="utf-8") as f:
                json.dump(msgs, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("Error saving seen status:", e)

    return updated

def save_chat_message(msg_obj):
    msgs = load_chat_messages()
    msgs.append(msg_obj)
    if len(msgs) > 100:
        msgs = msgs[-100:]
    with open(CHAT_FILE, "w", encoding="utf-8") as f:
        json.dump(msgs, f, ensure_ascii=False, indent=2)
    return msgs

class RoomConnectionManager:
    def __init__(self):
        # Maps websocket -> {"username": str, "in_voice": bool}
        self.connections: dict = {}

    async def connect(self, ws: WebSocket, username: str):
        await ws.accept()
        self.connections[ws] = {"username": username, "in_voice": False}
        await self.broadcast_user_list()

    def disconnect(self, ws: WebSocket):
        if ws in self.connections:
            del self.connections[ws]

    def get_online_users(self):
        return [info["username"] for info in self.connections.values()]

    def get_voice_users(self):
        return [info["username"] for info in self.connections.values() if info["in_voice"]]

    async def broadcast_user_list(self):
        msg = {
            "type": "room_state",
            "online_users": self.get_online_users(),
            "voice_users": self.get_voice_users()
        }
        await self.broadcast(msg)

    async def broadcast(self, message: dict):
        dead = []
        for ws in list(self.connections.keys()):
            try:
                await ws.send_text(json.dumps(message))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def send_to_user(self, target_username: str, message: dict):
        for ws, info in self.connections.items():
            if info["username"] == target_username:
                try:
                    await ws.send_text(json.dumps(message))
                except Exception:
                    self.disconnect(ws)

room_manager = RoomConnectionManager()

@app.delete("/api/courses")
@app.post("/api/courses/delete")
async def delete_course(request: Request, course_name: Optional[str] = None):
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Yetkisiz işlem! Yalnızca admin ders silebilir.")
        
    if not course_name:
        course_name = request.query_params.get("course_name")
        
    if not course_name:
        try:
            body = await request.json()
            if isinstance(body, dict):
                course_name = body.get("course_name")
        except Exception:
            pass
            
    if not course_name:
        try:
            form = await request.form()
            course_name = form.get("course_name")
        except Exception:
            pass

    if not course_name:
        raise HTTPException(status_code=400, detail="Silinecek ders adı belirtilmedi.")

    course_name = course_name.strip()
    remove_course_from_store(course_name)
    return {"status": "success", "message": f"'{course_name}' dersi ve tüm dosyaları başarıyla silindi."}

@app.post("/api/chat/seen")
async def api_chat_seen(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Lütfen önce giriş yapın.")
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    msg_ids = body.get("message_ids")
    updated = mark_messages_seen(user["username"], user.get("role", "user"), msg_ids)
    if updated:
        await room_manager.broadcast({
            "type": "messages_seen_update",
            "updates": updated
        })
    return {"status": "success", "updated_count": len(updated)}

@app.get("/api/chat/messages")
async def get_chat_messages(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Lütfen önce giriş yapın.")
    return {"messages": load_chat_messages()}

@app.get("/api/chat/media/{filename}")
async def get_chat_media(filename: str):
    safe_name = os.path.basename(filename)
    fpath = os.path.join(CHAT_MEDIA_DIR, safe_name)
    if not os.path.exists(fpath):
        raise HTTPException(status_code=404, detail="Medya bulunamadı")
    return FileResponse(fpath)

@app.post("/api/chat/upload")
async def upload_chat_media(request: Request, file: UploadFile = File(...)):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Lütfen önce giriş yapın.")

    orig_name = file.filename or "media"
    ext = os.path.splitext(orig_name)[1].lower()

    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp"}
    video_exts = {".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv"}

    if ext in image_exts:
        media_type = "image"
    elif ext in video_exts:
        media_type = "video"
    else:
        raise HTTPException(status_code=400, detail="Yalnızca fotoğraf veya video yükleyebilirsiniz (JPG, PNG, GIF, WEBP, MP4, WEBM).")

    safe_id = secrets.token_hex(8)
    clean_orig = re.sub(r'[^a-zA-Z0-9_.-]', '', orig_name)
    safe_fname = f"{safe_id}_{clean_orig}"
    target_path = os.path.join(CHAT_MEDIA_DIR, safe_fname)

    with open(target_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    file_url = f"/chat_media/{safe_fname}"
    return {
        "status": "success",
        "file_url": file_url,
        "media_type": media_type,
        "filename": orig_name
    }

@app.post("/api/chat/drive")
async def send_chat_drive(
    request: Request,
    drive_url: str = Form(...),
    title: Optional[str] = Form(None),
    note: Optional[str] = Form("")
):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Lütfen önce giriş yapın.")

    m = re.search(r'/d/([a-zA-Z0-9_-]+)', drive_url) or re.search(r'id=([a-zA-Z0-9_-]+)', drive_url)
    file_id = m.group(1) if m else drive_url.strip()

    clean_title = (title or "").strip() or "Google Drive Dosyası"
    clean_note = (note or "").strip()

    now_time = time.strftime("%H:%M")
    msg_obj = {
        "id": "msg_" + secrets.token_hex(6),
        "username": user["username"],
        "role": user.get("role", "user"),
        "text": clean_note,
        "media_url": drive_url.strip(),
        "media_type": "drive",
        "title": clean_title,
        "file_id": file_id,
        "time": now_time,
        "seen_by": [
            {
                "username": user["username"],
                "role": user.get("role", "user"),
                "time": now_time
            }
        ]
    }
    save_chat_message(msg_obj)
    await room_manager.broadcast({"type": "chat_message", "message": msg_obj})
    return {"status": "success", "message": msg_obj}

@app.post("/api/chat/send")
async def send_chat_message(
    request: Request,
    text: Optional[str] = Form(""),
    media_url: Optional[str] = Form(None),
    media_type: Optional[str] = Form(None),
    title: Optional[str] = Form(None),
    file_id: Optional[str] = Form(None)
):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Lütfen önce giriş yapın.")
    clean_text = (text or "").strip()
    if not clean_text and not media_url:
        raise HTTPException(status_code=400, detail="Mesaj veya medya boş olamaz.")
    
    now_time = time.strftime("%H:%M")
    msg_obj = {
        "id": "msg_" + secrets.token_hex(6),
        "username": user["username"],
        "role": user.get("role", "user"),
        "text": clean_text,
        "media_url": media_url or None,
        "media_type": media_type or None,
        "title": (title or "").strip() or None,
        "file_id": (file_id or "").strip() or None,
        "time": now_time,
        "seen_by": [
            {
                "username": user["username"],
                "role": user.get("role", "user"),
                "time": now_time
            }
        ]
    }
    save_chat_message(msg_obj)
    await room_manager.broadcast({"type": "chat_message", "message": msg_obj})
    return {"status": "success", "message": msg_obj}

@app.websocket("/ws/room")
async def websocket_room(websocket: WebSocket, token: Optional[str] = None):
    data = load_auth_data()
    username = None
    if token and token in data.get("sessions", {}):
        s_info = data["sessions"][token]
        username = s_info if isinstance(s_info, str) else s_info.get("username")
    
    if not username:
        await websocket.close(code=4001)
        return

    await room_manager.connect(websocket, username)
    try:
        while True:
            raw_text = await websocket.receive_text()
            try:
                msg = json.loads(raw_text)
                mtype = msg.get("type")

                if mtype == "chat":
                    text = (msg.get("text") or "").strip()
                    media_url = msg.get("media_url")
                    media_type = msg.get("media_type")
                    if text or media_url:
                        user_info = data.get("users", {}).get(username, {})
                        user_role = user_info.get("role", "user")
                        now_time = time.strftime("%H:%M")
                        title = msg.get("title")
                        file_id = msg.get("file_id")
                        msg_obj = {
                            "id": "msg_" + secrets.token_hex(6),
                            "username": username,
                            "role": user_role,
                            "text": text,
                            "media_url": media_url or None,
                            "media_type": media_type or None,
                            "title": (title or "").strip() or None,
                            "file_id": (file_id or "").strip() or None,
                            "time": now_time,
                            "seen_by": [
                                {
                                    "username": username,
                                    "role": user_role,
                                    "time": now_time
                                }
                            ]
                        }
                        save_chat_message(msg_obj)
                        await room_manager.broadcast({"type": "chat_message", "message": msg_obj})

                elif mtype == "chat_seen":
                    msg_ids = msg.get("message_ids")
                    user_info = data.get("users", {}).get(username, {})
                    user_role = user_info.get("role", "user")
                    updated = mark_messages_seen(username, user_role, msg_ids)
                    if updated:
                        await room_manager.broadcast({
                            "type": "messages_seen_update",
                            "updates": updated
                        })

                elif mtype == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))

                elif mtype == "voice_join":
                    if websocket in room_manager.connections:
                        room_manager.connections[websocket]["in_voice"] = True
                        await room_manager.broadcast_user_list()

                elif mtype == "voice_leave":
                    if websocket in room_manager.connections:
                        room_manager.connections[websocket]["in_voice"] = False
                        await room_manager.broadcast_user_list()

                elif mtype in ["webrtc_offer", "webrtc_answer", "webrtc_ice"]:
                    target = msg.get("target")
                    if target:
                        msg["sender"] = username
                        await room_manager.send_to_user(target, msg)

            except Exception as e:
                print("WS process error:", e)
    except WebSocketDisconnect:
        room_manager.disconnect(websocket)
        await room_manager.broadcast_user_list()
    except Exception:
        room_manager.disconnect(websocket)
        await room_manager.broadcast_user_list()

@app.post("/api/courses")
async def create_course(request: Request, course_name: str = Form(...)):
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Yetkisiz işlem! Yalnızca admin ders ekleyebilir.")
        
    clean_name = re.sub(r'[^a-zA-Z0-9_\-\.\sğüşıöçĞÜŞİÖÇ]', '', course_name).strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Geçersiz ders adı.")
        
    add_course_to_store(clean_name)
    return {"status": "success", "course": clean_name}

@app.post("/api/upload")
async def upload_pdf(request: Request, course: str = Form(...), file: UploadFile = File(...)):
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Yetkisiz işlem! Yalnızca admin PDF yükleyebilir.")
        
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Sadece PDF dosyaları yüklenebilir.")
    
    add_course_to_store(course)
    c_path = os.path.join(BASE_UPLOAD_DIR, course)
    os.makedirs(c_path, exist_ok=True)
    
    safe_name = re.sub(r'[^a-zA-Z0-9_\-\.ğüşıöçĞÜŞİÖÇ ]', '', file.filename)
    dest_path = os.path.join(c_path, safe_name)
    
    content = await file.read()
    with open(dest_path, "wb") as f:
        f.write(content)
        
    return JSONResponse({"status": "success", "message": "Dosya başarıyla yüklendi."})

@app.post("/api/upload-drive")
async def upload_drive_pdf(request: Request, course: str = Form(...), drive_url: str = Form(...), custom_title: Optional[str] = Form(None)):
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Yetkisiz işlem! Yalnızca admin PDF yükleyebilir.")

    m = re.search(r'/d/([a-zA-Z0-9_-]+)', drive_url) or re.search(r'id=([a-zA-Z0-9_-]+)', drive_url)
    if m:
        file_id = m.group(1)
    elif len(drive_url.strip()) > 20 and ' ' not in drive_url.strip():
        file_id = drive_url.strip()
    else:
        raise HTTPException(status_code=400, detail="Geçersiz Google Drive bağlantısı.")

    add_course_to_store(course)
    c_path = os.path.join(BASE_UPLOAD_DIR, course)
    os.makedirs(c_path, exist_ok=True)

    urls_to_try = [
        f"https://drive.usercontent.google.com/download?id={file_id}&export=download&authuser=0",
        f"https://drive.google.com/uc?export=download&id={file_id}"
    ]

    pdf_bytes = None
    content_disp = ""

    for u in urls_to_try:
        try:
            req = urllib.request.Request(
                u,
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                content_disp = resp.headers.get("Content-Disposition", "")
                pdf_bytes = resp.read()
                if pdf_bytes.startswith(b"%PDF"):
                    break
        except Exception:
            continue

    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF"):
        raise HTTPException(
            status_code=400, 
            detail="Dosya indirilemedi veya geçerli bir PDF değil. Lütfen dosyanın Google Drive'da 'Bağlantıya sahip olan herkes görüntüleyebilir' olarak paylaşıldığından emin olun."
        )

    if custom_title and custom_title.strip():
        fname = custom_title.strip()
        if not fname.lower().endswith(".pdf"):
            fname += ".pdf"
    elif "filename=" in content_disp:
        m_name = re.search(r'filename="?([^";]+)"?', content_disp)
        fname = m_name.group(1) if m_name else f"Drive_Notu_{file_id[:8]}.pdf"
    else:
        fname = f"Drive_Notu_{file_id[:8]}.pdf"

    safe_name = re.sub(r'[^a-zA-Z0-9_\-\.ğüşıöçĞÜŞİÖÇ ]', '', fname)
    dest_path = os.path.join(c_path, safe_name)

    with open(dest_path, "wb") as f:
        f.write(pdf_bytes)

    return JSONResponse({"status": "success", "message": f"'{safe_name}' başarıyla Google Drive'dan aktarıldı."})

@app.delete("/api/delete/{course}/{filename}")
async def delete_pdf(request: Request, course: str, filename: str):
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Yetkisiz işlem! Yalnızca admin silebilir.")
        
    fpath = os.path.join(BASE_UPLOAD_DIR, course, filename)
    if os.path.exists(fpath):
        os.remove(fpath)
        return {"status": "success", "message": "Dosya silindi."}
    raise HTTPException(status_code=404, detail="Dosya bulunamadı.")

@app.get("/api/view")
@app.get("/api/view/{course}/{filename}")
async def view_pdf(request: Request, course: Optional[str] = None, filename: Optional[str] = None, token: Optional[str] = None):
    # Support query params
    if not course:
        course = request.query_params.get("course")
    if not filename:
        filename = request.query_params.get("filename")
    if not token:
        token = request.query_params.get("token")

    user = get_current_user(request)
    if not user and token:
        data = load_auth_data()
        s_info = data.get("sessions", {}).get(token)
        if s_info:
            u = s_info if isinstance(s_info, str) else s_info.get("username")
            if u in data.get("users", {}):
                user = data["users"][u]

    if not user:
        raise HTTPException(status_code=401, detail="Lütfen önce giriş yapın.")

    if not course or not filename:
        raise HTTPException(status_code=400, detail="Ders veya dosya adı eksik.")

    fpath = os.path.join(BASE_UPLOAD_DIR, course, filename)
    if not os.path.exists(fpath):
        raise HTTPException(status_code=404, detail=f"'{filename}' dosyası sunucuda bulunamadı. Lütfen notu tekrar yükleyin.")
    
    def iterfile():
        with open(fpath, mode="rb") as file_like:
            yield from file_like
            
    return StreamingResponse(
        iterfile(),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{urllib.parse.quote(filename)}"',
            "Content-Type": "application/pdf"
        }
    )

@app.get("/api/download")
@app.get("/api/download/{course}/{filename}")
async def download_full(request: Request, course: Optional[str] = None, filename: Optional[str] = None, token: Optional[str] = None):
    if not course: course = request.query_params.get("course")
    if not filename: filename = request.query_params.get("filename")
    if not token: token = request.query_params.get("token")

    user = get_current_user(request)
    if not user and token:
        data = load_auth_data()
        s_info = data.get("sessions", {}).get(token)
        if s_info:
            u = s_info if isinstance(s_info, str) else s_info.get("username")
            if u in data.get("users", {}):
                user = data["users"][u]

    if not user:
        raise HTTPException(status_code=401, detail="Lütfen önce giriş yapın.")

    if not course or not filename:
        raise HTTPException(status_code=400, detail="Ders veya dosya adı eksik.")

    fpath = os.path.join(BASE_UPLOAD_DIR, course, filename)
    if not os.path.exists(fpath):
        raise HTTPException(status_code=404, detail=f"'{filename}' dosyası sunucuda bulunamadı.")
    
    def iterfile():
        with open(fpath, mode="rb") as file_like:
            yield from file_like
            
    return StreamingResponse(
        iterfile(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{urllib.parse.quote(filename)}"'}
    )

@app.get("/api/download-range")
@app.get("/api/download-range/{course}/{filename}")
async def download_range(request: Request, course: Optional[str] = None, filename: Optional[str] = None, pages: Optional[str] = None, token: Optional[str] = None):
    if not course: course = request.query_params.get("course")
    if not filename: filename = request.query_params.get("filename")
    if not pages: pages = request.query_params.get("pages")
    if not token: token = request.query_params.get("token")

    user = get_current_user(request)
    if not user and token:
        data = load_auth_data()
        s_info = data.get("sessions", {}).get(token)
        if s_info:
            u = s_info if isinstance(s_info, str) else s_info.get("username")
            if u in data.get("users", {}):
                user = data["users"][u]

    if not user:
        raise HTTPException(status_code=401, detail="Lütfen önce giriş yapın.")

    if not course or not filename or not pages:
        raise HTTPException(status_code=400, detail="Eksik parametreler.")

    fpath = os.path.join(BASE_UPLOAD_DIR, course, filename)
    if not os.path.exists(fpath):
        raise HTTPException(status_code=404, detail=f"'{filename}' dosyası sunucuda bulunamadı.")
    
    reader = PdfReader(fpath)
    total_pages = len(reader.pages)
    selected_indices = parse_range(pages, total_pages)
    
    if not selected_indices:
        raise HTTPException(status_code=400, detail="Geçersiz sayfa aralığı.")
        
    writer = PdfWriter()
    for idx in selected_indices:
        writer.add_page(reader.pages[idx])
        
    output_stream = io.BytesIO()
    writer.write(output_stream)
    output_stream.seek(0)
    
    out_name = f"{os.path.splitext(filename)[0]}_Sayfa_{pages.replace(' ', '')}.pdf"
    
    return StreamingResponse(
        output_stream,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{urllib.parse.quote(out_name)}"'}
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

# --- Keep-Alive / Anti-Sleep Ping Worker ---
@app.get("/api/ping")
async def api_ping():
    return {
        "status": "ok",
        "message": "Pong! Site 7/24 kesintisiz aktif.",
        "time": time.time(),
        "date": time.strftime("%Y-%m-%d %H:%M:%S")
    }

async def self_keep_alive():
    await asyncio.sleep(15)
    while True:
        try:
            ext_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("SITE_URL")
            if ext_url:
                ping_url = f"{ext_url.rstrip('/')}/api/ping"
                req = urllib.request.Request(ping_url, headers={"User-Agent": "RenderKeepAlive/1.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    pass
        except Exception:
            pass
        # Ping every 9 minutes (Render sleeps after 15 min inactivity)
        await asyncio.sleep(540)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(self_keep_alive())

# --- Admin Backup & Restore Endpoints ---
@app.get("/api/admin/backup")
async def download_backup(request: Request):
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Yalnızca admin yedek alabilir.")
    
    backup_data = {
        "timestamp": time.time(),
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "courses": get_all_courses(),
        "users": load_auth_data().get("users", {}),
        "chat_messages": load_chat_messages(),
    }
    content = json.dumps(backup_data, ensure_ascii=False, indent=2)
    filename = f"ders_portali_yedek_{time.strftime('%Y%m%d_%H%M')}.json"
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@app.post("/api/admin/restore")
async def upload_restore(request: Request, file: UploadFile = File(...)):
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Yalnızca admin yedek yükleyebilir.")
        
    try:
        content = await file.read()
        data = json.loads(content.decode("utf-8"))
        
        # Restore courses
        if "courses" in data and isinstance(data["courses"], list):
            with open(COURSES_FILE, "w", encoding="utf-8") as f:
                json.dump(data["courses"], f, ensure_ascii=False, indent=2)
            for c in data["courses"]:
                os.makedirs(os.path.join(BASE_UPLOAD_DIR, c), exist_ok=True)
                
        # Restore users
        if "users" in data and isinstance(data["users"], dict):
            auth_data = load_auth_data()
            auth_data["users"].update(data["users"])
            save_auth_data(auth_data)
            
        # Restore chat
        if "chat_messages" in data and isinstance(data["chat_messages"], list):
            with open(CHAT_FILE, "w", encoding="utf-8") as f:
                json.dump(data["chat_messages"], f, ensure_ascii=False, indent=2)
                
        return {"status": "success", "message": "Yedek başarıyla geri yüklendi!"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Yedek geri yüklenirken hata: {str(e)}")
