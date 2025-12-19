# 🤖 Slack Bot - KB File Search Integration

**Natural language Slack bot** that answers questions by searching your knowledge base through Gemini File Search, with citations and multi-section filtering.

## 🎯 What It Does

Users ask questions in **Slack DMs** → Bot searches your KB in **File Search Store** → Returns answers with **citations** and **emoji-formatted sections**.

```
User DM: "devrel: how to release notes?"
Bot Response:
🔗 *DEVREL*
The process involves...
_Fuentes:_
📄 kb/devrel/processes/process-release-notes.md
```

## 🚀 Quick Start

### 1. Install
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure
Create `.env`:
```env
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
GEMINI_API_KEY=...
FILE_SEARCH_STORE_NAMES=fileSearchStores/...
GEMINI_MODEL=gemini-2.5-flash
BUFFER_SECONDS=3.5
```

### 3. Create Slack App

In [slack.com/apps](https://api.slack.com/apps):

1. **Create App** → From scratch
2. **Name it** → "KB Bot"
3. **Socket Mode** → Enable
4. **Copy tokens** to `.env`:
   - `xoxb-...` → SLACK_BOT_TOKEN
   - `xapp-...` → SLACK_APP_TOKEN

5. **OAuth Scopes** (Tab: OAuth & Permissions):
   - `chat:write`
   - `im:history`

6. **Socket Mode** → Generate App-Level Token → Add to `.env`

### 4. Run
```bash
python3 bot.py
```

Expected output:
```
✅ Bot corriendo (Socket Mode)...
```

### 5. Test in Slack
Send DM to bot:
```
@stats      # Show expected documents
@audit      # Show real documents
What is the incident process?
```

## 📝 Files

| File | Purpose |
|------|---------|
| `bot.py` | Slack listener + command handler |
| `gemini_kb.py` | Gemini File Search integration |
| `setup_store.py` | Store creation helper |

## 🎮 Commands

### Special Commands
```
@stats   - Show expected documents (from sync_state.json)
@audit   - Show real documents (from Google API)
```

### Multi-Section Queries
```
growth: What's the strategy?
devrel: Release process?
handbook: How do we work?
incidents: Triage checklist?
```

Supported sections:
- `incidents` (or `incident`)
- `devrel`
- `growth`
- `handbook`
- `organization`
- `shared`
- `changelog`

### Normal Questions
```
How do we handle emergencies?
What's in the glossary?
Show me contribution guidelines
```

## 🏗️ Architecture

```
User DM (Slack)
     ↓
bot.py (listener)
     ↓
parse_multi_sections() (parse filters)
     ↓
gemini_kb.answer() (File Search)
     ↓
Format with emoji + sources
     ↓
chat_postMessage() (reply)
```

### Key Functions

**`bot.py`:**
- `@app.event("message")` - DM listener
- `_get_special_command_response()` - Handles @stats, @audit
- `_get_answer_response()` - Processes questions
- `_flush()` - Sends buffered response after delay

**`gemini_kb.py`:**
- `answer(question, metadata_filter)` - Query with File Search
- `get_store_stats()` - Expected documents count
- `get_store_audit()` - Real documents count

## 🔄 How It Works

1. **Listen** - User sends DM
2. **Buffer** - Wait 3.5 seconds for more messages (avoid double-post)
3. **Parse** - Extract section filters if present
4. **Query** - Search File Search Store with Gemini
5. **Format** - Add emoji, citations, sources
6. **Reply** - Send formatted response

## 🎨 Features

### Emoji by Section
- 🚨 incidents
- 👨‍💻 devrel
- 📈 growth
- 📖 handbook
- 🏢 organization
- 🔗 shared
- 📚 default

### Source Citations
Automatically includes document sources:
```
_Fuentes:_
📄 kb/incidents/playbook-incident-management-framework.md
📄 kb/handbook/guide-github-contribution.md
```

### Anti-Duplicate Buffering
- 3.5 second buffer prevents double-posts from rapid typing
- Duplicate event detection by `client_msg_id`

## 🔗 Integration with Handbook KB

This bot queries the **File Search Store** that Handbook project maintains:

```
Handbook project: kb/ → sync_kb_to_store.py → File Search Store
                                                      ↑
Slack bot: queries via Gemini ←←←←←←←←←←←←←←←←←←←←
```

**Requirements:**
- Handbook project must be synced first
- Store ID must be in `.env`
- 14 documents indexed and ACTIVE

## 🐛 Troubleshooting

| Issue | Solution |
|-------|----------|
| "Bot not responding" | Check SLACK_BOT_TOKEN, SLACK_APP_TOKEN, Socket Mode enabled |
| "@stats shows error" | Handbook sync_state.json must exist, check path in gemini_kb.py |
| "@audit shows fewer docs | Documents in PENDING state don't appear, wait for indexing |
| "No sources shown" | Ensure documents have custom_metadata with "path" key |
| Slack app token expired | Regenerate in Socket Mode, update `.env` |

## 🔐 Security

- `.env` git-ignored (safe for secrets)
- Tokens scoped to needed permissions only
- Only reads File Search Store (no write access)
- Socket Mode (no public webhook)

## 📊 Metrics

- **DM Processing** ~1-2 seconds
- **Query Latency** ~500ms (File Search)
- **Duplicate Prevention** 3.5s buffer
- **Event Deduplication** 2 minute TTL

## 🚀 Ready for Production

- ✅ All scripts compile
- ✅ Slack integration tested
- ✅ Error handling comprehensive
- ✅ KB integration verified
- ✅ Commands working

## 📚 Resources

- [Slack Bolt Documentation](https://slack.dev/bolt-python/)
- [Gemini File Search API](https://ai.google.dev/api/rest)
- [Socket Mode Guide](https://api.slack.com/socket-mode)
- [Related Handbook KB](../Handbook_MVP_File_Search)

## 🎯 Next Steps

1. Create Slack App with tokens
2. Set up `.env` with all credentials
3. Run `python bot.py`
4. Test special commands (@stats, @audit)
5. Test normal questions
6. Deploy to server or Cloud Function

---

**Version:** 2.0.0 (Production Ready)  
**Last Updated:** 2024-12-18  
**Status:** ✅ Slack integration complete, KB sync verified
