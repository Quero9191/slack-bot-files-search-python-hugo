import os
import time
import threading
import re
from pathlib import Path

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from gemini_kb import answer, get_store_stats, get_store_audit


# Cargar .env
env_path = Path(__file__).with_name(".env")
load_dotenv(env_path)

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
BUFFER_SECONDS = float(os.getenv("BUFFER_SECONDS", "3.5"))

if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
    raise RuntimeError("Faltan SLACK_BOT_TOKEN / SLACK_APP_TOKEN en .env")

app = App(token=SLACK_BOT_TOKEN)

# Buffer simple por canal (sin Redis)
_lock = threading.Lock()
_timers = {}     # channel -> Timer
_last_text = {}  # channel -> text
_seen_event_ids = {}
SEEN_TTL_SECONDS = 120  # 2 minutos

_last_post_ts = {}
POST_COOLDOWN_SECONDS = 0.8  # evita doble post muy seguido


def parse_multi_sections(text: str):
    """
    Soporta varias preguntas en el mismo mensaje:
    growth: ...
    devrel: ...
    handbook: ...
    """
    t = (text or "").strip()

    alias_to_section = {
        "incident": "incidents",
        "incidents": "incidents",
        "growth": "growth",
        "devrel": "devrel",
        "handbook": "handbook",
        "organization": "organization",
        "shared": "shared",
        "changelog": "changelog",
    }

    # Exigimos ":" para separar bien cuando hay varias
    pattern = r"(?i)\b(incident|incidents|growth|devrel|handbook|organization|shared|changelog)\s*:\s*"
    matches = list(re.finditer(pattern, t))

    # Si no hay prefijos, devolvemos todo como una sola pregunta sin filtro
    if not matches:
        return [(None, t, None)]

    parts = []
    for i, m in enumerate(matches):
        raw = m.group(1).lower()
        section = alias_to_section.get(raw)

        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(t)
        q = t[start:end].strip()

        if section and q:
            parts.append((f'section="{section}"', q, section))

    # Fallback de seguridad: si algo salió raro, que no pete
    if not parts:
        return [(None, t, None)]

    return parts


def _get_special_command_response(cmd: str) -> str | None:
    """Maneja comandos especiales (stats, audit). Retorna msg si es especial, None si no."""
    if cmd not in ["stats", "@stats", "!stats", "audit", "@audit", "!audit"]:
        return None
    
    try:
        if cmd.lower() in ["stats", "@stats", "!stats"]:
            stats = get_store_stats()
            if "error" in stats:
                return f"❌ Error obteniendo stats: {stats['error']}"
            
            total = stats.get("total_documents", 0)
            docs = stats.get("documents", [])
            msg = f"📊 *KB Store Statistics (Expected)*\n\n"
            msg += f"📚 *Total: {total} documentos*\n"
            
            if docs:
                msg += "\n_Documentos en sync_state.json:_\n"
                for doc in sorted(docs):
                    doc_name = doc.split("/")[-1]
                    section = doc.split("/")[1] if "/" in doc else "unknown"
                    msg += f"• `{doc_name}` (__{section}__)\n"
            
            return msg
        
        elif cmd.lower() in ["audit", "@audit", "!audit"]:
            audit = get_store_audit()
            if "error" in audit:
                return f"❌ Error en audit: {audit['error']}"
            
            real = audit.get("real_documents", 0)
            msg = f"🔍 *KB Store Audit (Real State)*\n\n"
            msg += f"📚 *Documentos REALES en Google: {real}*\n\n"
            msg += f"✅ Sincronización OK" if real > 0 else "⚠️ Store vacío o inaccesible"
            return msg
    
    except Exception as e:
        return f"⚠️ Error: {e}"
    
    return None


def _get_answer_response(text: str) -> str:
    """Procesa la pregunta normal y retorna el texto formateado"""
    try:
        parts = parse_multi_sections(text)
        blocks = []

        for metadata_filter, clean_text, label in parts:
            text_out, sources = answer(clean_text, metadata_filter=metadata_filter)

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

            # Agregar fuentes con formato mejorado
            if sources and not re.search(r"(?im)(fuentes|sources|references):\s", block):
                sources_formatted = "\n".join([f"📄 {s}" for s in sources])
                block += f"\n\n_Fuentes:_\n{sources_formatted}"

            blocks.append(block)

        return "\n\n" + "─" * 40 + "\n\n".join(blocks)

    except Exception as e:
        return f"⚠️ Error: {type(e).__name__}: {e}"
    """Detecta si ya hemos visto este evento (evita duplicados)"""
    global _seen_event_ids
    
    # client_msg_id suele venir en mensajes de usuario
    event_id = event.get("client_msg_id") or event.get("event_ts") or event.get("ts")
    if not event_id:
        return False

    now = time.time()

    # limpieza de IDs antiguos (fuera del lock para evitar bloqueos)
    expired = [k for k, t0 in _seen_event_ids.items() if now - t0 > SEEN_TTL_SECONDS]
    for k in expired:
        _seen_event_ids.pop(k, None)

    if event_id in _seen_event_ids:
        return True

    _seen_event_ids[event_id] = now
    return False


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


@app.event("message")
def on_message(event, logger):
    """Listener para mensajes directos al bot"""
    # Ignora bots / subtypes
    if event.get("bot_id") or event.get("subtype"):
        return
    if is_duplicate_event(event):
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


if __name__ == "__main__":
    print("✅ Bot corriendo (Socket Mode)...")
    SocketModeHandler(app, SLACK_APP_TOKEN).start()