from __future__ import annotations

import os
import tempfile
from functools import lru_cache
from pathlib import Path

import fitz
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile


app = FastAPI(title="AI ASR File OCR Service", version="0.1.0")


def _env_int(name: str, fallback: int) -> int:
    try:
        return int(os.getenv(name, str(fallback)))
    except ValueError:
        return fallback


OCR_SERVICE_TOKEN = os.getenv("OCR_SERVICE_TOKEN", "").strip()
OCR_LANG = os.getenv("OCR_LANG", "ch").strip() or "ch"
OCR_MAX_PAGES = _env_int("OCR_MAX_PAGES", 80)
OCR_RENDER_SCALE = float(os.getenv("OCR_RENDER_SCALE", "2.0") or "2.0")


def _require_token(authorization: str | None) -> None:
    if not OCR_SERVICE_TOKEN:
        return
    expected = f"Bearer {OCR_SERVICE_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="OCR_UNAUTHORIZED")


@lru_cache(maxsize=1)
def _ocr_engine():
    from paddleocr import PaddleOCR

    return PaddleOCR(use_angle_cls=True, lang=OCR_LANG, show_log=False)


@app.get("/health")
def health():
    return {"status": "ok", "engine": "paddleocr", "lang": OCR_LANG, "max_pages": OCR_MAX_PAGES}


@app.post("/api/ocr/pdf")
async def ocr_pdf(
    file: UploadFile = File(...),
    max_pages: int | None = Form(None),
    authorization: str | None = Header(default=None),
):
    _require_token(authorization)
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="EMPTY_FILE")
    page_limit = min(max_pages or OCR_MAX_PAGES, OCR_MAX_PAGES)
    pages = []
    warnings: list[str] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            document = fitz.open(stream=content, filetype="pdf")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"PDF_OPEN_FAILED: {exc}") from exc
        if len(document) > page_limit:
            warnings.append(f"文件共 {len(document)} 页，本次仅 OCR 前 {page_limit} 页。")
        engine = _ocr_engine()
        matrix = fitz.Matrix(OCR_RENDER_SCALE, OCR_RENDER_SCALE)
        for page_index in range(min(len(document), page_limit)):
            page = document.load_page(page_index)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            image_path = Path(tmpdir) / f"page_{page_index + 1}.png"
            pixmap.save(str(image_path))
            result = engine.ocr(str(image_path), cls=True)
            lines = _flatten_ocr_lines(result)
            pages.append({"page_number": page_index + 1, "text": "\n".join(lines), "line_count": len(lines)})
    text = "\n\n".join(f"## 第 {page['page_number']} 页 OCR文本\n{page['text']}" for page in pages if page["text"]).strip()
    return {
        "engine": "PaddleOCR",
        "lang": OCR_LANG,
        "page_count": len(pages),
        "text": text,
        "pages": pages,
        "warnings": warnings,
    }


def _flatten_ocr_lines(result) -> list[str]:
    lines: list[str] = []
    for block in result or []:
        for item in block or []:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            value = item[1]
            if isinstance(value, (list, tuple)) and value:
                text = str(value[0]).strip()
                if text:
                    lines.append(text)
    return lines
