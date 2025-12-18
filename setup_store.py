import os
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai

# Cargar .env
env_path = Path(__file__).with_name(".env")
load_dotenv(env_path)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

if not GEMINI_API_KEY:
    raise RuntimeError("Falta GEMINI_API_KEY en .env")

client = genai.Client(api_key=GEMINI_API_KEY)

# 1) Crear store
store = client.file_search_stores.create(config={"display_name": "kb-test-store"})
print("✅ Store creado:", store.name)

# 2) Crear un archivo de prueba
sample_path = Path(__file__).with_name("sample_kb.md")
sample_path.write_text(
    """---
title: "Refunds checklist"
maintainer: "Support Team"
last_updated: "2025-12-17"
---

# Refunds checklist
- Confirm identity
- Check eligibility
- Process refund
""",
    encoding="utf-8"
)
print("✅ Archivo de prueba creado:", sample_path)

# 3) Subir al store (el display_name es lo que suele salir en “Fuentes”) :contentReference[oaicite:0]{index=0}
op = client.file_search_stores.upload_to_file_search_store(
    file=str(sample_path),
    file_search_store_name=store.name,
    config={
        "display_name": "operations/support/checklists/checklist-refunds.md",
        "custom_metadata": [
            {"key": "department", "string_value": "operations"},
            {"key": "team", "string_value": "support"},
            {"key": "doc_type", "string_value": "checklist"},
        ],
    },
)

# Esperar a que termine
while not op.done:
    print("...indexando (espera 3s)")
    time.sleep(3)
    op = client.operations.get(op)

print("✅ Import terminado. Ya puedes usar el store:", store.name)