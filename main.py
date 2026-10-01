import os
import io
import re
import json
import time
import base64
import secrets
import hashlib
import urllib.request
from typing import List, Optional
from fastapi import FastAPI, Form, UploadFile, File, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from pypdf import PdfReader, PdfWriter

app = FastAPI(title="Ders Notları & PDF Portalı")

BASE_UPLOAD_DIR = "uploads"
AUTH_FILE = "users.json"
SESSION_TIMEOUT_SECONDS = 24 * 3600  # 24 saatlik aktiflik süresi
os.makedirs(BASE_UPLOAD_DIR, exist_ok=True)

DEFAULT_COURSES = ["Kadın Doğum Hemşireliği", "İç Hastalıkları", "Cerrahi Hastalıkları", "Sağlık Tanılaması", "Genel"]
for course in DEFAULT_COURSES:
    os.makedirs(os.path.join(BASE_UPLOAD_DIR, course), exist_ok=True)

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
    token = request.cookies.get("session_token")
    if not token:
        auth_header = request.headers.get("authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]

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
    # 24 saat içinde tekrar girmediyse oturumu sonlandır
    if now - last_active > SESSION_TIMEOUT_SECONDS:
        del data["sessions"][token]
        save_auth_data(data)
        return None

    # 24 saat içinde tekrar girdiyse süreyi 24 saat daha uzat (sliding expiration)
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
    courses = [d for d in os.listdir(BASE_UPLOAD_DIR) if os.path.isdir(os.path.join(BASE_UPLOAD_DIR, d))]
    courses.sort()
    
    for course in courses:
        c_path = os.path.join(BASE_UPLOAD_DIR, course)
        for fname in os.listdir(c_path):
            if fname.lower().endswith(".pdf"):
                fpath = os.path.join(c_path, fname)
                size = os.path.getsize(fpath)
                try:
                    reader = PdfReader(fpath)
                    page_count = len(reader.pages)
                except Exception:
                    page_count = 1
                data.append({
                    "course": course,
                    "filename": fname,
                    "title": os.path.splitext(fname)[0].replace("_", " "),
                    "size": size,
                    "page_count": page_count
                })
    return {"courses": courses, "files": data}

@app.post("/api/upload")
async def upload_pdf(request: Request, course: str = Form(...), file: UploadFile = File(...)):
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Yetkisiz işlem! Yalnızca admin PDF yükleyebilir.")
        
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Sadece PDF dosyaları yüklenebilir.")
    
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

@app.get("/api/view/{course}/{filename}")
async def view_pdf(request: Request, course: str, filename: str):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Lütfen önce giriş yapın.")

    fpath = os.path.join(BASE_UPLOAD_DIR, course, filename)
    if not os.path.exists(fpath):
        raise HTTPException(status_code=404, detail="Dosya bulunamadı.")
    
    def iterfile():
        with open(fpath, mode="rb") as file_like:
            yield from file_like
            
    return StreamingResponse(
        iterfile(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'}
    )

@app.get("/api/download/{course}/{filename}")
async def download_full(request: Request, course: str, filename: str):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Lütfen önce giriş yapın.")

    fpath = os.path.join(BASE_UPLOAD_DIR, course, filename)
    if not os.path.exists(fpath):
        raise HTTPException(status_code=404, detail="Dosya bulunamadı.")
    
    def iterfile():
        with open(fpath, mode="rb") as file_like:
            yield from file_like
            
    return StreamingResponse(
        iterfile(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@app.get("/api/download-range/{course}/{filename}")
async def download_range(request: Request, course: str, filename: str, pages: str):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Lütfen önce giriş yapın.")

    fpath = os.path.join(BASE_UPLOAD_DIR, course, filename)
    if not os.path.exists(fpath):
        raise HTTPException(status_code=404, detail="Dosya bulunamadı.")
    
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
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'}
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
