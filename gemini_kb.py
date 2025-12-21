import os
import json
from typing import List, Tuple
from pathlib import Path

from google import genai
from google.genai import types
import time
import requests
import threading


SYSTEM_INSTRUCTION = """
Eres el asistente interno del KB (Handbook). Tu trabajo es responder preguntas usando
exclusivamente la base de conocimiento indexada por File Search y la metadata asociada.
Sigue estas reglas estrictamente:

1) Prioridad de fuentes
- Siempre usa evidencias extraídas del resultado de File Search (grounding). Si una
    respuesta puede ser fundada en fragmentos recuperados, da primero una respuesta
    breve y exacta, seguida de una sección "Fuentes:" con la lista de paths o títulos
    exactos (máx. 6 fuentes).
- Si no hay grounding suficiente, responde: "No hay suficiente información en el KB"
    y solicita más contexto.

2) Estilo y longitud
- Modo por defecto: conciso — 5–8 líneas en el idioma del usuario.
- Si la pregunta pide "detallar" o "explicar paso a paso", responde en modo extendido
    (hasta 12–18 líneas) y ofrece pasos numerados.

3) Formato de respuesta
- Respuesta principal (texto).
- Si hay fuentes: añade un bloque exacto "Fuentes:" seguido de líneas con el path
    relativo del documento (ej. kb/handbook/overview.md) y, entre paréntesis, la sección
 /frontmatter relevante si está disponible.

4) Metadata y filtros
- Respeta `metadata_filter` pasado por la llamada. Si se pasa, prioriza resultados
    dentro de ese filtro y deja claro que se filtró por esa sección.
- Si los metadatos (`owner`, `last_review`, `review_cycle_days`) influyen, menciónalo
    brevemente.

5) Manejo de ambigüedad y multi-sección
- Si el input contiene múltiples consultas, trata cada una por separado y devuelve
    bloques separados con su título/emoji.

6) Errores y fallback
- Si la API falla, sólo si `ALLOW_LOCAL_SYNC_STATE_FALLBACK=1` comienza la respuesta con
    "⚠️ Fallback local — API inaccesible." y luego muestra contenido tomado de archivos
    locales.
- Nunca inventes fuentes. Si no puedes citar, di que es inferencia.

7) Seguridad
- No expongas secretos ni rutas absolutas; sólo paths relativos del KB.

8) Comandos especiales
- Para `audit`: devolver `real_documents: N` y la lista de documentos (path + owner si existe).

9) Metadatos en respuesta
- Si `last_review` excede `review_cycle_days` o falta, añade: "Nota: este documento podría
    estar desactualizado (last_review: YYYY-MM-DD)".

10) Lenguaje
- Responde en el idioma del usuario detectado.

Razonamiento: prioriza evidencia, estructura salidas, obliga a citar fuentes y gestiona fallback.
""".strip()


# In-memory cache + simple lock to avoid stampedes (per-process)
_store_stats_cache = None
_store_stats_cache_ts = 0
_store_stats_lock = threading.Lock()


def get_store_stats() -> dict:
    """
    Obtiene estadísticas del KB usando la API (fuente de la verdad).
    - Cache en memoria con TTL configurable (`STORE_STATS_CACHE_TTL`).
    - Evita stampede con un lock simple por proceso.
    - Si la API falla, puede hacer fallback a `sync_state.json` sólo si
      `ALLOW_LOCAL_SYNC_STATE_FALLBACK=1` está activado (por seguridad).
    """
    global _store_stats_cache, _store_stats_cache_ts, _store_stats_lock

    try:
        ttl = int(os.getenv("STORE_STATS_CACHE_TTL", "30"))
    except Exception:
        ttl = 30

    now = int(time.time())

    # Fast path: cache válida
    if _store_stats_cache is not None and (now - _store_stats_cache_ts) < ttl:
        return _store_stats_cache

    # Intentamos refrescar tomando el lock sin bloquear demasiado
    got_lock = _store_stats_lock.acquire(blocking=False)
    if got_lock:
        try:
            # doble check
            now = int(time.time())
            if _store_stats_cache is not None and (now - _store_stats_cache_ts) < ttl:
                return _store_stats_cache

            api_key = os.getenv("GEMINI_API_KEY")
            stores_raw = os.getenv("FILE_SEARCH_STORE_NAMES", "")
            stores = [s.strip() for s in stores_raw.split(",") if s.strip()]

            if not api_key or not stores:
                raise RuntimeError("Config missing: GEMINI_API_KEY or FILE_SEARCH_STORE_NAMES")

            client = genai.Client(api_key=api_key)

            total = 0
            docs_list = []
            for store_name in stores:
                docs = client.file_search_stores.documents.list(parent=store_name)
                for d in docs:
                    total += 1
                    try:
                        meta = {m.key: m.string_value for m in d.custom_metadata}
                    except Exception:
                        meta = {}
                    display = meta.get("path") or getattr(d, "name", "")
                    docs_list.append({"id": getattr(d, "name", ""), "path": display, "metadata": meta})

            result = {"total_documents": total, "documents": docs_list}

            _store_stats_cache = result
            _store_stats_cache_ts = int(time.time())
            return result

        except Exception as e:
            # Si la API falla, permitir fallback local sólo si la variable lo autoriza
            allow_local = os.getenv("ALLOW_LOCAL_SYNC_STATE_FALLBACK", "0") == "1"
            if allow_local:
                try:
                    handbook_path = Path(os.getenv("SYNC_STATE_PATH", 
                                                   "/Users/quero/Downloads/Scripts_VSCode/Handbook_MVP_File_Search/sync_state.json"))
                    if handbook_path.exists():
                        with open(handbook_path) as f:
                            state = json.load(f)
                        result = {"total_documents": len(state), "documents": list(state.keys()), "fallback": True, "error": str(e)}
                        _store_stats_cache = result
                        _store_stats_cache_ts = int(time.time())
                        return result
                except Exception:
                    pass
            return {"error": str(e)}

        finally:
            try:
                _store_stats_lock.release()
            except Exception:
                pass

    else:
        # Otro hilo/proceso está refrescando. Esperamos un poco por la cache.
        waited = 0.0
        wait_interval = 0.05
        max_wait = min(ttl, 5)
        while waited < max_wait:
            if _store_stats_cache is not None:
                return _store_stats_cache
            time.sleep(wait_interval)
            waited += wait_interval

        # Timeout: intentamos una llamada directa (sin lock) como último recurso
        try:
            api_key = os.getenv("GEMINI_API_KEY")
            stores_raw = os.getenv("FILE_SEARCH_STORE_NAMES", "")
            stores = [s.strip() for s in stores_raw.split(",") if s.strip()]
            if not api_key or not stores:
                return {"error": "Config missing: GEMINI_API_KEY or FILE_SEARCH_STORE_NAMES"}
            client = genai.Client(api_key=api_key)
            total = 0
            docs_list = []
            for store_name in stores:
                docs = client.file_search_stores.documents.list(parent=store_name)
                for d in docs:
                    total += 1
                    try:
                        meta = {m.key: m.string_value for m in d.custom_metadata}
                    except Exception:
                        meta = {}
                    display = meta.get("path") or getattr(d, "name", "")
                    docs_list.append({"id": getattr(d, "name", ""), "path": display, "metadata": meta})

            result = {"total_documents": total, "documents": docs_list}
            _store_stats_cache = result
            _store_stats_cache_ts = int(time.time())
            return result
        except Exception as e:
            allow_local = os.getenv("ALLOW_LOCAL_SYNC_STATE_FALLBACK", "0") == "1"
            if allow_local:
                try:
                    handbook_path = Path(os.getenv("SYNC_STATE_PATH", "/Users/quero/Downloads/Scripts_VSCode/Handbook_MVP_File_Search/sync_state.json"))
                    if handbook_path.exists():
                        with open(handbook_path) as f:
                            state = json.load(f)
                        result = {"total_documents": len(state), "documents": list(state.keys()), "fallback": True, "error": str(e)}
                        _store_stats_cache = result
                        _store_stats_cache_ts = int(time.time())
                        return result
                except Exception:
                    pass
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
        docs_list = []
        for store_name in stores:
            docs = client.file_search_stores.documents.list(parent=store_name)
            for d in docs:
                total_docs += 1
                try:
                    meta = {m.key: m.string_value for m in d.custom_metadata}
                except Exception:
                    meta = {}
                display = meta.get("path") or getattr(d, "name", "")
                docs_list.append({"id": getattr(d, "name", ""), "path": display, "metadata": meta})
        return {"real_documents": total_docs, "documents": docs_list}
    except Exception as e:
        # Si la API falla, intentar fallback leyendo `sync_state.json` local
        try:
            handbook_path = Path(os.getenv("SYNC_STATE_PATH", "/Users/quero/Downloads/Scripts_VSCode/Handbook_MVP_File_Search/sync_state.json"))
            if handbook_path.exists():
                with open(handbook_path) as f:
                    state = json.load(f)
                docs_list = []
                total_docs = 0
                # `state` mapea path -> {hash, store_doc_id}
                for p, v in state.items():
                    total_docs += 1
                    docs_list.append({
                        "id": v.get("store_doc_id") if isinstance(v, dict) else None,
                        "path": p,
                        "metadata": v if isinstance(v, dict) else {},
                    })
                return {"real_documents": total_docs, "documents": docs_list, "fallback": True, "error": str(e)}
        except Exception:
            pass

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


def _local_answer_fallback(question: str, max_results: int = 3) -> Tuple[str, List[str]]:
    """Fallback simple: busca en los archivos locales del KB usando `sync_state.json`.
    Devuelve (texto_agregado, [paths])
    """
    try:
        import re
        sync_path = Path(os.getenv("SYNC_STATE_PATH", "/Users/quero/Downloads/Scripts_VSCode/Handbook_MVP_File_Search/sync_state.json"))
        kb_root = Path(os.getenv("KB_ROOT", "/Users/quero/Downloads/Scripts_VSCode/Handbook_MVP_File_Search/kb"))

        if not sync_path.exists():
            return ("", [])

        with open(sync_path) as f:
            state = json.load(f)

        q = (question or "").lower()
        tokens = re.findall(r"\w+", q)

        results = []  # (score, path, content)
        for p in state.keys():
            try:
                # Normalize path: state keys often include a leading 'kb/'
                rel = p
                if rel.startswith("kb/"):
                    rel = rel[len("kb/"):]
                file_path = kb_root.joinpath(rel)
                if not file_path.exists():
                    # fallback try without leading slash
                    file_path = kb_root.joinpath(rel.lstrip("/"))
                if not file_path.exists():
                    continue
                txt = file_path.read_text(encoding="utf-8")
                tl = txt.lower()
                score = 0
                for t in tokens:
                    if not t:
                        continue
                    score += tl.count(t)
                # also bump if whole question phrase found
                if q and q in tl:
                    score += 5
                if score > 0:
                    results.append((score, str(p), txt))
            except Exception:
                continue

        # If no results by token-match, pick up to max_results by file size heuristic
        if not results:
            fallback = []
            for p in state.keys():
                try:
                    file_path = kb_root.joinpath(p)
                    if not file_path.exists():
                        file_path = kb_root.joinpath(p.lstrip("/"))
                    if not file_path.exists():
                        continue
                    txt = file_path.read_text(encoding="utf-8")
                    fallback.append((len(txt), str(p), txt))
                except Exception:
                    continue
            fallback.sort(reverse=True)
            results = [(s, p, t) for s, p, t in fallback[:max_results]]

        # Sort by score desc
        results.sort(key=lambda x: x[0], reverse=True)

        snippets = []
        sources = []
        for _, p, txt in results[:max_results]:
            snippet = txt.strip()
            if len(snippet) > 4000:
                snippet = snippet[:4000] + "..."
            snippets.append(snippet)
            sources.append(p)

        if not snippets:
            return ("", [])

        out_text = "\n\n---\n\n".join(snippets)
        prefix = "⚠️ Fallback local (API inaccesible). Contenido tomado de archivos locales:\n\n"
        return (prefix + out_text, sources)
    except Exception:
        return ("", [])


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

    try:
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
    except Exception as e:
        # Si la API de generación/filtrado falla, intentar fallback local
        allow_local = os.getenv("ALLOW_LOCAL_SYNC_STATE_FALLBACK", "0") == "1"
        if allow_local:
            local_text, local_sources = _local_answer_fallback(question)
            if local_text:
                # For MVP UX: do not expose local fallback sources as "Fuentes".
                # Only show sources when they come from File Search grounding.
                return local_text, []
        # Re-raise original error if no fallback
        raise