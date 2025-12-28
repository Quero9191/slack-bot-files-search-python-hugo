import os
import time
import logging
import threading
import re
from pathlib import Path
from gemini_kb import answer, get_store_audit
import json
import uuid
from typing import Optional

from gsheets_feedback import append_feedback_row

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

# --- Logging setup: write to `logs/bot.log` with rotation (keeps logging minimal/INFO)
from logging.handlers import RotatingFileHandler
LOG_DIR = Path(__file__).parent / "logs"
try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "bot.log"
    rh = RotatingFileHandler(str(log_file), maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    rh.setFormatter(fmt)
    root_logger = logging.getLogger()
    # If no handlers configured, set level and add file handler. Keep existing handlers if present.
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(rh)
except Exception:
    # If logging setup fails, continue without file logging
    logging.exception("Failed to configure file logging")
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

# Feedback settings
_last_feedback_time: dict = {}
_answer_context: dict = {}  # stores prompt/response context by message_ts
FEEDBACK_COOLDOWN_SECONDS = float(os.getenv("FEEDBACK_COOLDOWN_SECONDS", "30"))
FEEDBACK_SHEET_ID = os.getenv("FEEDBACK_SHEET_ID")
FEEDBACK_SECRETS_PATH = os.getenv("FEEDBACK_SECRETS_PATH", "./secrets")

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
                    msg += f"• 📄 `{name}` (__{section}__)\n"

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
                sources_formatted = "\n".join([f"📄 {s}" for s in sources])
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

    # Post as Block with an action button for feedback
    answer_id = uuid.uuid4().hex
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": final_text}}
    ]
    try:
        button_value = json.dumps({"answer_id": answer_id})
    except Exception:
        button_value = answer_id

    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Dar feedback"},
                "action_id": "open_feedback_modal",
                "value": button_value
            }
        ]
    })

    try:
        res = app.client.chat_postMessage(channel=channel, blocks=blocks, text=(final_text[:3000] or "response"))
        message_ts = res.get("ts")
        # Store context for later retrieval in feedback modal
        if message_ts:
            _answer_context[message_ts] = {
                "prompt": text,
                "response": final_text,
                "answer_id": answer_id
            }
    except Exception as e:
        logging.exception("Failed to post message with blocks; falling back to text: %s", e)
        app.client.chat_postMessage(channel=channel, text=final_text)
        return


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


@app.action("open_feedback_modal")
def handle_open_feedback_modal(ack, body, client, logger):
    """Opens a Slack modal to collect feedback when the user clicks the feedback button."""
    try:
        ack()
        trigger_id = body.get("trigger_id")
        user_id = body.get("user", {}).get("id")

        # Try to preserve context: message ts and channel are in the action payload
        message = body.get("message", {})
        channel_id = body.get("channel", {}).get("id") or message.get("channel")
        message_ts = message.get("ts")

        # value may contain answer_id
        value = {}
        try:
            value = json.loads(body.get("actions", [])[0].get("value") or "{}")
        except Exception:
            value = {}

        answer_id = value.get("answer_id") or str(uuid.uuid4().hex)

        # Get stored context (prompt/response)
        ctx = _answer_context.get(message_ts, {})
        prompt = ctx.get("prompt", "")
        response = ctx.get("response", "")

        # Truncate long text for display in modal
        prompt_display = (prompt[:500] + "...") if len(prompt) > 500 else prompt
        response_display = (response[:500] + "...") if len(response) > 500 else response

        view = {
            "type": "modal",
            "callback_id": "feedback_view",
            "private_metadata": json.dumps({"answer_id": answer_id, "channel": channel_id, "message_ts": message_ts}),
            "title": {"type": "plain_text", "text": "Enviar feedback"},
            "submit": {"type": "plain_text", "text": "Enviar"},
            "close": {"type": "plain_text", "text": "Cancelar"},
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "Gracias por tu feedback — cuéntanos qué mejorar."}},
                {"type": "divider"},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*Tu pregunta:*\n```\n{prompt_display}\n```"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*Respuesta del bot:*\n```\n{response_display}\n```"}},
                {"type": "divider"},
                {"type": "input", "block_id": "rating_block", "label": {"type": "plain_text", "text": "Calificación"}, "element": {"type": "static_select", "action_id": "rating_action", "placeholder": {"type": "plain_text", "text": "Selecciona una opción"}, "options": [
                    {"text": {"type": "plain_text", "text": "5 — Muy útil"}, "value": "5"},
                    {"text": {"type": "plain_text", "text": "4 — Útil"}, "value": "4"},
                    {"text": {"type": "plain_text", "text": "3 — Regular"}, "value": "3"},
                    {"text": {"type": "plain_text", "text": "2 — Poco útil"}, "value": "2"},
                    {"text": {"type": "plain_text", "text": "1 — Malo"}, "value": "1"}
                ]}},
                {"type": "input", "block_id": "comment_block", "label": {"type": "plain_text", "text": "Comentario"}, "element": {"type": "plain_text_input", "action_id": "comment_action", "multiline": True, "placeholder": {"type": "plain_text", "text": "Escribe aquí tu comentario..."}}, "optional": True}
            ]
        }

        client.views_open(trigger_id=trigger_id, view=view)
    except Exception as e:
        logger.exception("open_feedback_modal failed: %s", e)


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


@app.view("feedback_view")
def handle_feedback_submission(ack, body, client, logger):
    """Handles submission of the feedback modal and writes to Google Sheets."""
    try:
        # Acknowledge the view_submission to Slack immediately
        ack()

        user_id = body.get("user", {}).get("id")
        view = body.get("view", {})
        state = view.get("state", {}).get("values", {})
        private_metadata = {}
        try:
            private_metadata = json.loads(view.get("private_metadata") or "{}")
        except Exception:
            private_metadata = {}

        rating = None
        comment = None
        # Extract values from state
        rb = state.get("rating_block", {})
        if rb:
            sel = rb.get("rating_action") or {}
            rating = sel.get("selected_option", {}).get("value")

        cb = state.get("comment_block", {})
        if cb:
            txt = cb.get("comment_action") or {}
            comment = txt.get("value")

        # Get user info (name and email)
        username = ""
        email = ""
        try:
            user_info = client.users_info(user=user_id)
            if user_info.get("ok"):
                user_obj = user_info.get("user", {})
                username = user_obj.get("real_name") or user_obj.get("name") or ""
                email = user_obj.get("profile", {}).get("email") or ""
        except Exception as e:
            logger.warning("Failed to get user info for %s: %s", user_id, e)

        # Get stored context
        message_ts = private_metadata.get("message_ts")
        ctx = _answer_context.get(message_ts, {})
        prompt = ctx.get("prompt", "")
        response = ctx.get("response", "")

        row = {
            "timestamp": int(time.time()),
            "username": username,
            "email": email,
            "prompt": prompt,
            "response": response,
            "rating": rating,
            "comment": comment,
            "fallback": False
        }

        # Store channel for ephemeral messages
        channel_id = private_metadata.get("channel")

        # cooldown per user
        now = time.time()
        last = _last_feedback_time.get(user_id, 0)
        if now - last < FEEDBACK_COOLDOWN_SECONDS:
            client.chat_postEphemeral(channel=channel_id or user_id, user=user_id, text=f"⏳ Por favor espera {int(FEEDBACK_COOLDOWN_SECONDS - (now-last))}s antes de enviar otro feedback.")
            return

        try:
            append_feedback_row(row, sheet_id=FEEDBACK_SHEET_ID)
            _last_feedback_time[user_id] = now
            client.chat_postEphemeral(channel=channel_id or user_id, user=user_id, text="✅ Gracias — tu feedback se ha registrado.")
        except Exception as e:
            logger.exception("Failed to append feedback row: %s", e)
            client.chat_postEphemeral(channel=channel_id or user_id, user=user_id, text="⚠️ No se ha podido guardar el feedback. Inténtalo más tarde.")

    except Exception as e:
        logger.exception("Error handling feedback submission: %s", e)


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