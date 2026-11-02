#!/usr/bin/env bash
# Chạy slog3grade bằng Python trong .venv của skill (nếu có), giữ nguyên tham số. Dùng: run.sh <thư mục> [tuỳ chọn]
skill="$(cd "$(dirname "$0")/.." && pwd)"
py="$skill/.venv/bin/python"; [ -x "$py" ] || py=python3
cd "$skill" && PYTHONIOENCODING=utf-8 exec "$py" -m slog3grade "$@"
