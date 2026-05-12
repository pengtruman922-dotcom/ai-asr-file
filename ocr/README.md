# OCR Service

独立 Railway OCR 服务，用于扫描 PDF 的 PaddleOCR 识别。

## Railway 服务设置

- Root Directory: `ocr`
- Build: Dockerfile 自动构建
- Start Command: 留空，使用 Dockerfile CMD
- Variables:
  - `PORT=8000`
  - `OCR_SERVICE_TOKEN`: 建议设置为随机长字符串
  - `OCR_LANG`: 默认 `ch`
  - `OCR_MAX_PAGES`: 默认 `80`
  - `OCR_RENDER_SCALE`: 默认 `2.0`

Web/Worker 服务需要增加：

- `OCR_SERVICE_URL=http://${{OCR.RAILWAY_PRIVATE_DOMAIN}}:${{OCR.PORT}}`
- `OCR_SERVICE_TOKEN=${{OCR.OCR_SERVICE_TOKEN}}`
- `OCR_SERVICE_TIMEOUT_SECONDS=900`

其中 `OCR` 是你在 Railway 中给 OCR 服务设置的服务名。
