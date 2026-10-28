# Capcut (YT Downloader + OpenCut)

App desktop chạy trên Windows, gồm:

- **Tải video YouTube** bằng yt-dlp, chọn được bản lồng tiếng (audio theo ngôn ngữ), phụ đề, chỉ âm thanh.
- **Chỉnh sửa video** bằng [OpenCut](https://github.com/opencut-app/opencut-classic) nhúng sẵn trong app (khung hình, chữ, font, logo...).
- **Xuất hàng loạt**: thiết kế một project mẫu trong OpenCut, rồi xuất nhiều video theo mẫu đó cùng lúc.
  - Video trên track chính của project được thay bằng từng video mới.
  - Chữ `{title}` được thay bằng tên file video.

## Cấu trúc

```
app/        App desktop (Python + pywebview): server.py, editor.py, oc_launcher.py, index.html
opencut/    Mã nguồn OpenCut (bản classic, MIT) + phần xuất hàng loạt (apps/web/src/batch)
_tools/     Python, Node, Bun, ffmpeg cài cục bộ (không đưa lên git)
yt-dlp/     Mã nguồn yt-dlp (không đưa lên git)
```

## Cài đặt

1. Đặt các công cụ vào `_tools/`:
   - `_tools/Python314/` (Python 3.14), cài thêm `pywebview`
   - `_tools/node/node.exe`
   - `_tools/bun/`
   - `_tools/ffmpeg/` (ffmpeg.exe, ffprobe.exe)
2. Clone yt-dlp vào `yt-dlp/`:
   ```
   git clone https://github.com/yt-dlp/yt-dlp yt-dlp
   ```
3. Build OpenCut:
   ```
   cd opencut
   bun install
   copy apps\web\.env.example apps\web\.env.local   # rồi sửa NEXT_PUBLIC_SITE_URL=http://127.0.0.1:3917
   powershell -File ..\app\build_opencut.ps1
   ```
4. Chạy app:
   ```
   _tools\Python314\pythonw.exe app\server.py
   ```

## Giấy phép

OpenCut thuộc bản quyền OpenCut, phát hành theo giấy phép MIT (xem `opencut/LICENSE`).
