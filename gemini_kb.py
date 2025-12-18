import os
import json
from typing import List, Tuple
from pathlib import Path

from google import genai
from google.genai import types
import requests


SYSTEM_INSTRUCTION = """
Eres un asistente interno.
- Responde en el mismo idioma del usuario.
- Usa File Search (KB) para responder.
- Sé MUY conciso por defecto: 5-8 líneas máximo.
- Si la pregunta es amplia, da un resumen y ofrece ampliar ("Puedo detallarte X si quieres").
- Si no hay suficiente info en el KB, dilo y pide contexto.
""".strip()


def get_store_stats() -> dict:
    """
    Obtiene estadísticas del KB leyendo sync_state.json del repositorio.
    Esto es más confiable que la API REST porque tiene el estado real.
    """
    try:
        handbook_path = Path("/Users/quero/Downloads/Scripts_VSCode/Handbook_MVP_File_Search/sync_state.json")
        
        if not handbook_path.exists():
            return {"error": "sync_state.json not found"}
        
        with open(handbook_path) as f:
            state = json.load(f)
        
        return {
            "total_documents": len(state),
            "documents": list(state.keys())
        }
    except Exception as e:
        return {"error": str(e)}


def get_store_audit() -> dict:
    """
    Audita el Store REAL consultando directamente la API de Google.
    Muestra el estado actual real de los documentos.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    stores_raw = os.getenv("FILE_SEARCH_STORE_NAMES", "")
    stores = [s.strip() for s in stores_raw.split(",") if s.strip()]
    
    if not api_key or not stores:
        return {"error": "Config missing"}
    
    try:
        client = genai.Client(api_key=api_key)
        
        total_docs = 0
        for store_name in stores:
            docs = client.file_search_stores.documents.list(parent=store_name)
            total_docs += len(list(docs))
        
        return {"real_documents": total_docs}
    except Exception as e:
        return {"error": str(e)}


def _extract_sources(resp) -> List[str]:
    # En File Search, las citas se exponen via grounding_metadata. :contentReference[oaicite:2]{index=2}
    out = []
    try:
        gm = resp.candidates[0].grounding_metadata
        chunks = getattr(gm, "grounding_chunks", None) or []
        for ch in chunks:
            rc = getattr(ch, "retrieved_context", None)
            if not rc:
                continue
            title = getattr(rc, "title", None) or ""
            uri = getattr(rc, "uri", None) or ""
            label = (title or uri).strip()
            if label:
                out.append(label)
    except Exception:
        return []

    # unique manteniendo orden
    seen = set()
    uniq = []
    for s in out:
        if s not in seen:
            uniq.append(s)
            seen.add(s)
    return uniq


def answer(question: str, metadata_filter: str | None = None) -> Tuple[str, List[str]]:
    api_key = os.getenv("GEMINI_API_KEY")
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    stores_raw = os.getenv("FILE_SEARCH_STORE_NAMES", "")
    stores = [s.strip() for s in stores_raw.split(",") if s.strip()]

    if not api_key:
        raise RuntimeError("Falta GEMINI_API_KEY en .env")
    if not stores:
        raise RuntimeError("Falta FILE_SEARCH_STORE_NAMES en .env")

    client = genai.Client(api_key=api_key)

    tool = types.Tool(
        file_search=types.FileSearch(
            file_search_store_names=stores,
            # Ejemplo simple de filtro por metadata: :contentReference[oaicite:3]{index=3}
            # metadata_filter='department="operations" AND team="support"'
            metadata_filter=metadata_filter,
        )
    )

    resp = client.models.generate_content(
        model=model,
        contents=question,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            tools=[tool],
            temperature=0.2,
        ),
    )

    text = (resp.text or "").strip()
    sources = _extract_sources(resp)
    return text, sources