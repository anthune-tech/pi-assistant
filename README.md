# PI Assistant

A local AI personal assistant with voice capabilities, running on Linux/macOS/Windows. Manages schedules, quotations, projects, reminders, and chat with a local LLM.

## Features
- **Chat** — Conversational AI via local LLM (llama-cpp-python)
- **Voice** — Speech-to-text (Google/Vosk) + Text-to-speech (Piper)
- **Schedule** — Meeting management with auto-reminders
- **Quotations** — Track pending quotes with due date alerts
- **Projects** — Organize projects with sub-tasks and deadline warnings
- **Dashboard** — Overview of all upcoming items
- **Memory** — Remembers facts and preferences

## Quick Start
```bash
pip install -r requirements.txt
mkdir models
# Download LLM model into models/
python app.py
```

See `installation.txt` for detailed setup.
