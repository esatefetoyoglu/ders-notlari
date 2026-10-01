import os
import io
import re
from typing import List
from fastapi import FastAPI, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from pypdf import PdfReader, PdfWriter

app = FastAPI(title="Ders Notları & PDF Portalı")

BASE_UPLOAD_DIR = "uploads"
os.makedirs(BASE_UPLOAD_DIR, exist_ok=True)

DEFAULT_COURSES = ["Kadın Doğum Hemşireliği", "İç Hastalıkları", "Cerrahi Hastalıkları", "Sağlık Tanılaması", "Genel"]
for course in DEFAULT_COURSES:
    os.makedirs(os.path.join(BASE_UPLOAD_DIR, course), exist_ok=True)

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

@app.get("/", response_class=HTMLResponse)
async def home():
    template_path = os.path.join("templates", "index.html")
    if not os.path.exists(template_path):
        template_path = "index.html"
    with open(template_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/api/files")
async def list_files():
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
async def upload_pdf(course: str = Form(...), file: UploadFile = File(...)):
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

@app.get("/api/view/{course}/{filename}")
async def view_pdf(course: str, filename: str):
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
async def download_full(course: str, filename: str):
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
async def download_range(course: str, filename: str, pages: str):
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
