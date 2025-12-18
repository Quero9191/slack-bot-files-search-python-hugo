import os
from typing import List, Tuple

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
    """Obtiene estadísticas del Store (número de documentos, etc)"""
    api_key = os.getenv("GEMINI_API_KEY")
    stores_raw = os.getenv("FILE_SEARCH_STORE_NAMES", "")
    stores = [s.strip() for s in stores_raw.split(",") if s.strip()]
    
    if not api_key or not stores:
        return {"error": "Config missing"}
    
    stats = {}
    base_url = "https://generativelanguage.googleapis.com/v1beta"
    
    for store_name in stores:
        doc_count = 0
        page_token = None
        
        try:
            while True:
                params = {"key": api_key, "pageSize": 50}
                if page_token:
                    params["pageToken"] = page_token
                
                url = f"{base_url}/{store_name}/documents"
                resp = requests.get(url, params=params, timeout=10)
                
                if resp.status_code == 400:
                    break
                resp.raise_for_status()
                
                data = resp.json()
                docs = data.get("documents", [])
                doc_count += len(docs)
                
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
        except Exception as e:
            return {"error": str(e)}
        
        stats[store_name.split("/")[-1]] = doc_count
    
    return stats


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