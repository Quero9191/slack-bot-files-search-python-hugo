import os
import time
import logging
import threading
import re
from pathlib import Path
from gemini_kb import answer, get_store_audit
import json

# load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# Slack app initialization (reads tokens from env or .env)
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
app = App(token=SLACK_BOT_TOKEN) if SLACK_BOT_TOKEN else App()

# Runtime globals for buffering / dedupe
_lock = threading.Lock()
_last_text: dict = {}
_timers: dict = {}
_last_post_ts: dict = {}
_seen_event_ids: dict = {}

# Tunables (env override)
POST_COOLDOWN_SECONDS = float(os.getenv("POST_COOLDOWN_SECONDS", "2.0"))
SEEN_TTL_SECONDS = int(os.getenv("SEEN_TTL_SECONDS", "60"))
BUFFER_SECONDS = float(os.getenv("BUFFER_SECONDS", "3.5"))
DOCS_BASE_URL = os.getenv("DOCS_BASE_URL", "")

# Section inference index (built lazily)
_SECTION_INDEX = None  # token -> set(sections)
_SECTIONS = None


def build_section_index(sync_state_path: str | Path = None):
    """Construye un índice simple token -> sections a partir de `sync_state.json`.
    Utiliza los nombres de fichero y la ruta para generar tokens asociados a cada sección.
    """
    global _SECTION_INDEX, _SECTIONS
    if _SECTION_INDEX is not None and _SECTIONS is not None:
        return

    if sync_state_path is None:
        sync_state_path = os.getenv("SYNC_STATE_PATH", "/Users/quero/Downloads/Scripts_VSCode/Handbook_MVP_File_Search/sync_state.json")

    try:
        p = Path(sync_state_path)
        if not p.exists():
            _SECTION_INDEX = {}
            _SECTIONS = set()
            return

        raw = json.loads(p.read_text(encoding="utf-8"))
        idx = {}
        secs = set()
        for fullpath in raw.keys():
            parts = fullpath.split("/")
            if len(parts) < 2:
                continue
            section = parts[1]
            secs.add(section)
            name = parts[-1]
            # tokens from filename and section
            tokens = re.findall(r"\w+", name.lower())
            tokens += re.findall(r"\w+", section.lower())
            for t in tokens:
                if not t:
                    continue
                idx.setdefault(t, set()).add(section)

        _SECTION_INDEX = idx
        _SECTIONS = secs
    except Exception:
        _SECTION_INDEX = {}
        _SECTIONS = set()


def infer_section_from_text(text: str) -> str | None:
    """Infieres la sección más probable desde `text` usando el índice.
    Regresa el nombre de la sección o None si no hay suficiente evidencia.
    """
    try:
        build_section_index()
        if not _SECTION_INDEX:
            return None

        q = (text or "").lower()
        words = re.findall(r"\w+", q)
        scores = {}
        # direct match of section name has high weight
        for s in _SECTIONS:
            if s and re.search(rf"\b{re.escape(s)}\b", q, flags=re.I):
                scores[s] = scores.get(s, 0) + 5

        for w in words:
            secs = _SECTION_INDEX.get(w)
            if not secs:
                continue
            for s in secs:
                scores[s] = scores.get(s, 0) + 1

        if not scores:
            return None

        # pick best
        best, best_score = max(scores.items(), key=lambda kv: kv[1])
        # threshold: need at least 2 points or direct match
        if best_score >= 2:
            return best
        return None
    except Exception:
        return None


def _get_special_command_response(text: str) -> str | None:
    """Maneja comandos especiales como `audit`.
    Retorna un mensaje formateado o None si no es un comando especial.
    """
    try:
        if not text:
            return None

        if text.strip() in ("audit", "kb audit", "store audit"):
            audit = get_store_audit()
            if isinstance(audit, dict) and "error" in audit:
                return f"❌ Error en audit: {audit['error']}"

            real = audit.get("real_documents", 0)
            docs = audit.get("documents", [])
            msg = f"🔍 *KB Store Audit (Real State)*\n\n"
            msg += f"📚 *Documentos REALES en Google: {real}*\n\n"

            if docs:
                msg += "_Documentos:_\n"
                for d in docs:
                    path = d.get("path") or ""
                    name = path.split("/")[-1] if path else d.get("id", "unknown")
                    section = path.split("/")[1] if path and "/" in path else "unknown"
                    display = name
                    if path and (path.startswith("http://") or path.startswith("https://")):
                        link = f"<{path}|{display}>"
                    elif path and DOCS_BASE_URL:
                        url = DOCS_BASE_URL.rstrip("/") + "/" + path.lstrip("/")
                        link = f"<{url}|{display}>"
                    else:
                        link = display
                    msg += f"• 📄 {link} (__{section}__)\n"

            msg += "\n✅ Audit completado"
            return msg

        return None
    except Exception as e:
        return f"⚠️ Error: {e}"


def parse_multi_sections(text: str):
    """Parsea consultas con prefijo de sección opcional.
    Devuelve lista de tuplas: (metadata_filter, clean_text, label)
    Ejemplos:
      "incidents: cómo..." -> ({'section': 'incidents'}, 'cómo...', 'incidents')
      "pregunta normal" -> (None, 'pregunta normal', None)
    """
    if not text:
        return [(None, "", None)]

    m = re.match(r"^([A-Za-z0-9_-]+):\s*(.+)$", text.strip())
    if m:
        label = m.group(1).lower()
        rest = m.group(2).strip()
        # Return metadata_filter as a string expression accepted by File Search
        mf = f'section="{label}"'
        return [(mf, rest, label)]

    return [(None, text.strip(), None)]


def _get_answer_response(text: str) -> str:
    """Procesa la pregunta normal y retorna el texto formateado"""
    try:
        parts = parse_multi_sections(text)
        blocks = []

        for metadata_filter, clean_text, label in parts:
            try:
                # If no explicit section/label provided, try to infer from the text
                if not metadata_filter:
                    inferred = infer_section_from_text(clean_text)
                    if inferred:
                        metadata_filter = f'section="{inferred}"'
                        label = inferred

                # If metadata_filter is a dict (old format), convert to string
                if isinstance(metadata_filter, dict):
                    parts = [f'{k}="{v}"' for k, v in metadata_filter.items()]
                    metadata_filter = " AND ".join(parts)

                text_out, sources = answer(clean_text, metadata_filter=metadata_filter)
            except Exception as e:
                text_out = f"⚠️ Error consultando el KB: {type(e).__name__}: {e}"
                sources = []

            if not text_out:
                text_out = "❓ No he encontrado info suficiente en el KB. ¿Puedes dar más contexto?"

            # Formatear la sección con emoji
            if label:
                emoji_map = {
                    "incidents": "🚨",
                    "devrel": "👨‍💻",
                    "growth": "📈",
                    "handbook": "📖",
                    "organization": "🏢",
                    "shared": "🔗"
                }
                emoji = emoji_map.get(label.lower(), "📚")
                block = f"{emoji} *{label.upper()}*\n{text_out}"
            else:
                block = text_out

            # Agregar fuentes con formato mejorado (si no existen ya en el bloque)
            if sources and not re.search(r"(?im)(fuentes|sources|references):\s", block):
                formatted_sources = []
                for s in sources:
                    try:
                        s_str = str(s)
                    except Exception:
                        s_str = s

                    # if already a URL, link directly; else, if DOCS_BASE_URL set, build URL
                    if s_str.startswith("http://") or s_str.startswith("https://"):
                        link = f"<{s_str}|{s_str}>"
                    elif DOCS_BASE_URL:
                        url = DOCS_BASE_URL.rstrip("/") + "/" + s_str.lstrip("/")
                        link = f"<{url}|{s_str}>"
                    else:
                        link = s_str
                    formatted_sources.append(f"📄 {link}")

                sources_formatted = "\n".join(formatted_sources)
                block += f"\n\n_Fuentes:_\n{sources_formatted}"

            blocks.append(block)

        # Return blocks joined with a blank line; remove visual separator line
        return "\n\n".join(blocks)

    except Exception as e:
        return f"⚠️ Error: {type(e).__name__}: {e}"
    


def _flush(channel: str):
    """Procesa el texto acumulado y envía la respuesta"""
    with _lock:
        text = _last_text.pop(channel, "").strip()
        _t = _timers.pop(channel, None)

    if not text:
        return

    # Intentar comando especial primero
    special_response = _get_special_command_response(text.lower())
    if special_response:
        final_text = special_response
    else:
        # Respuesta normal con IA
        final_text = _get_answer_response(text)

    # Anti doble-post cooldown
    now = time.time()
    last = _last_post_ts.get(channel, 0)
    if now - last < POST_COOLDOWN_SECONDS:
        return
    _last_post_ts[channel] = now

    app.client.chat_postMessage(channel=channel, text=final_text)


def is_duplicate_event(event) -> bool:
    """Detecta si ya hemos visto este evento (evita duplicados). Retorna True si está visto."""
    global _seen_event_ids

    # client_msg_id suele venir en mensajes de usuario
    event_id = event.get("client_msg_id") or event.get("event_ts") or event.get("ts")
    if not event_id:
        return False

    now = time.time()

    # limpieza de IDs antiguos
    expired = [k for k, t0 in _seen_event_ids.items() if now - t0 > SEEN_TTL_SECONDS]
    for k in expired:
        _seen_event_ids.pop(k, None)

    if event_id in _seen_event_ids:
        return True

    _seen_event_ids[event_id] = now
    return False


@app.event("message")
def on_message(event, logger):
    """Listener para mensajes directos al bot (con manejo de errores para evitar crash)."""
    try:
        # Ignora bots / subtypes
        if event.get("bot_id") or event.get("subtype"):
            return
        if is_duplicate_event(event):
            # opcional: logger.debug("Duplicate event ignored: %s", event)
            return

        # SOLO DM
        if event.get("channel_type") != "im":
            return

        channel = event.get("channel")
        text = (event.get("text") or "").strip()
        if not channel or not text:
            return

        with _lock:
            _last_text[channel] = text
            if channel in _timers:
                _timers[channel].cancel()

            t = threading.Timer(BUFFER_SECONDS, _flush, args=(channel,))
            t.daemon = True
            _timers[channel] = t
            t.start()

    except Exception as e:
        # Log the exception and attempt to notify the user in-channel
        try:
            print(f"[ERROR] on_message failed: {type(e).__name__}: {e}")
            ch = event.get("channel") if isinstance(event, dict) else None
            if ch:
                app.client.chat_postMessage(channel=ch, text=f"⚠️ Error interno: {type(e).__name__}: {e}")
        except Exception:
            # nothing much we can do here
            pass


if __name__ == "__main__":
    logging.info("✅ Bot corriendo (Socket Mode)...")
    print("🤖 Bot encendido ✅")

    # Run Socket Mode handler with ping and auto-restart loop to improve stability
    handler = None
    try:
        while True:
            try:
                handler = SocketModeHandler(app, SLACK_APP_TOKEN, ping_interval=5)
                handler.start()
            except KeyboardInterrupt:
                # Graceful stop on Ctrl-C without trace
                print("🛑 Bot parado con éxito (Ctrl-C detectado).")
                break
            except Exception:
                logging.exception("Socket Mode cayó; reiniciando en 5s…")
                time.sleep(5)
    except KeyboardInterrupt:
        print("🛑 Bot parado con éxito (Ctrl-C detectado).")
    finally:
        try:
            if handler is not None and hasattr(handler, "stop"):
                handler.stop()
        except Exception:
            pass
        print("🛑 Bot detenido.")