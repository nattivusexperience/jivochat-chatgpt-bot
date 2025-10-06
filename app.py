from flask import Flask, request, jsonify
from openai import OpenAI
import os, glob, json, math, requests

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False  # respuestas JSON en UTF-8 real

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# =========================
# Configuración general
# =========================
EMBED_MODEL = "text-embedding-3-small"
CHARS_PER_CHUNK = 800
KB_BASE = "kb"  # carpeta base de la KB

# Toggle encendido/apagado del bot
BOT_ENABLED = os.getenv("BOT_ENABLED", "true").lower() == "true"
try:
    BOT_ENABLED_MAP = json.loads(os.getenv("BOT_ENABLED_MAP", "{}"))  # {"token": true/false}
except Exception:
    BOT_ENABLED_MAP = {}

# Tokens permitidos (para /bot/<token>) y su "grupo" (opcional)
try:
    TOKEN_MAP = json.loads(os.getenv("JIVO_TOKEN_MAP", "{}"))
except Exception:
    TOKEN_MAP = {}

# provider_id por token (cuando Jivo te lo envíe)
try:
    PROVIDER_MAP = json.loads(os.getenv("JIVO_PROVIDER_MAP", "{}"))
except Exception:
    PROVIDER_MAP = {}

def jivo_endpoint_for(token: str) -> str:
    provider_id = PROVIDER_MAP.get(token, "")
    return f"https://bot.jivosite.com/webhooks/{provider_id}/{token}" if provider_id else None

def bot_message(chat_id: str, text: str, token: str):
    ep = jivo_endpoint_for(token)
    if not ep:
        print("Sin PROVIDER_ID aún: no puedo enviar BOT_MESSAGE.")
        return
    payload = {"event": "BOT_MESSAGE", "chat_id": chat_id, "message": {"type": "TEXT", "text": text}}
    try:
        r = requests.post(ep, json=payload, timeout=8)
        print("BOT_MESSAGE ->", r.status_code, r.text[:200])
    except Exception as e:
        print("Error BOT_MESSAGE ->", repr(e))

def invite_agent(chat_id: str, token: str):
    ep = jivo_endpoint_for(token)
    if not ep:
        print("Sin PROVIDER_ID aún: no puedo enviar INVITE_AGENT.")
        return
    payload = {"event": "INVITE_AGENT", "chat_id": chat_id}
    try:
        r = requests.post(ep, json=payload, timeout=8)
        print("INVITE_AGENT ->", r.status_code, r.text[:200])
    except Exception as e:
        print("Error INVITE_AGENT ->", repr(e))

# =========================
# Utilidades vectoriales
# =========================
def dot(a, b): return sum(x*y for x, y in zip(a, b))
def norm(v):   return math.sqrt(sum(x*x for x in v))
def cos_sim(a, b):
    na, nb = norm(a), norm(b)
    return 0.0 if na == 0 or nb == 0 else dot(a, b) / (na * nb)

# =========================
# Carga/Troceado de KB
# =========================
def brand_folder(brand: str) -> str:
    if brand:
        candidate = os.path.join(KB_BASE, brand)
        if os.path.isdir(candidate):
            return candidate
    return KB_BASE

def index_path_for(brand: str) -> str:
    safe = (brand or "general").replace("/", "_")
    return f"kb_index_{safe}.json"

def load_kb_files(kb_folder: str):
    docs = []
    # 1) .md
    for path in sorted(glob.glob(os.path.join(kb_folder, "*.md"))):
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
        title = os.path.basename(path)
        for i in range(0, len(text), CHARS_PER_CHUNK):
            chunk = text[i:i+CHARS_PER_CHUNK].strip()
            if chunk:
                docs.append({"title": title, "text": chunk})
    # 2) .jsonl
    for path in sorted(glob.glob(os.path.join(kb_folder, "*.jsonl"))):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                content = (rec.get("content") or "").strip()
                # Normalización suave de emails con espacios
                content = content.replace(" @", "@").replace("@ ", "@")
                title = f"{os.path.basename(path)}::{rec.get('id') or rec.get('title') or 'chunk'}"
                if content:
                    docs.append({"title": title, "text": content})
    return docs

def build_index_for(brand: str):
    folder = brand_folder(brand)
    docs = load_kb_files(folder)
    if not docs:
        return []
    inputs = [d["text"] for d in docs]
    emb = client.embeddings.create(model=EMBED_MODEL, input=inputs)
    for d, e in zip(docs, emb.data):
        d["embedding"] = e.embedding
    idx_path = index_path_for(brand)
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump(docs, f, ensure_ascii=False)
    return docs

def load_or_build_index_for(brand: str):
    idx_path = index_path_for(brand)
    if os.path.exists(idx_path):
        try:
            with open(idx_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return build_index_for(brand)

# caché de índices por marca
INDEX_CACHE = {}

def get_index(brand: str):
    key = brand or "general"
    if key not in INDEX_CACHE:
        INDEX_CACHE[key] = load_or_build_index_for(brand)
    return INDEX_CACHE[key]

# =========================
# Recuperación con fallback y empujes bilingües
# =========================
def keyword_hits(index_docs, keywords, limit=3):
    hits = []
    kws = [k.lower() for k in keywords]
    for d in index_docs:
        txt = (d["title"] + " " + d["text"]).lower()
        if any(k in txt for k in kws):
            hits.append((0.99, d))
            if len(hits) >= limit:
                break
    return hits

def _push_keyword_hits(index_docs, hits, ql, triggers, targets, limit=3):
    """Empuja candidatos por keywords si se activan los triggers en la query."""
    if any(k in ql for k in triggers):
        k_hits = keyword_hits(index_docs, targets, limit=limit)
        seen = {id(d) for _, d in hits}
        k_hits = [(s, d) for s, d in k_hits if id(d) not in seen]
        hits = k_hits + hits
    return hits

def retrieve_context(query, brand=None, top_k=8, min_sim=0.50):
    index_docs = get_index(brand)
    if not index_docs:
        return []

    q_emb = client.embeddings.create(model=EMBED_MODEL, input=[query]).data[0].embedding

    scored = []
    for d in index_docs:
        s = cos_sim(q_emb, d["embedding"])
        scored.append((s, d))
    scored.sort(key=lambda x: x[0], reverse=True)

    # Permisivo si parece inglés (para recall multilingüe)
    ql = (query or "").lower()
    is_en_like = any(m in ql for m in [" how ", "how much", "price", "cost", "what", "when", "where", "is there", "do you", "terms of use", "applicable law", "jurisdiction"])
    local_min_sim = 0.45 if is_en_like else min_sim

    hits = [(s, d) for s, d in scored[:top_k] if s >= local_min_sim]
    if not hits and scored:
        hits = scored[:2]  # siempre algo

    # Empujes existentes: "incluye"
    if ("incluye" in ql) or ("qué incluye" in ql) or ("que incluye" in ql):
        k_hits = keyword_hits(index_docs, ["incluye"], limit=2)
        seen = {id(d) for _, d in hits}
        k_hits = [(s, d) for s, d in k_hits if id(d) not in seen]
        hits = k_hits + hits

    # Empuje PRECIOS (sin 2026 para no colar itinerarios/fechas)
        if any(k in ql for k in ["precio", "precios", "coste", "price", "prices", "rate", "tarifa", "how much", "€"]):
        k_hits = keyword_hits(index_docs, ["precio", "precios", "tarifa", "price", "prices", "rate", "€"], limit=5)
        seen = {id(d) for _, d in hits}
        k_hits = [(s, d) for s, d in k_hits if id(d) not in seen]
        # Prioriza documentos cuyo título sugiera precios
        k_hits.sort(key=lambda sd: ("precio" in sd[1]["title"].lower() or "price" in sd[1]["title"].lower()), reverse=True)
        hits = k_hits + hits

   # Empuje 2026 (separado, favoreciendo precios si existen)
   if "2026" in ql:
        k_hits = keyword_hits(index_docs, ["2026"], limit=5)
        seen = {id(d) for _, d in hits}
       k_hits = [(s, d) for s, d in k_hits if id(d) not in seen]
  # Si el título NO contiene "precio"/"price", quedan detrás
        k_hits.sort(key=lambda sd: ("precio" in sd[1]["title"].lower() or "price" in sd[1]["title"].lower()), reverse=True)
       hits = k_hits + hits

    # NUEVOS empujes bilingües (ES/EN)
    # Jurisdicción / Applicable law
    hits = _push_keyword_hits(index_docs, hits, ql,
                              triggers=["jurisdicción", "juzgados", "competent court", "competent courts", "applicable law", "jurisdiction", "ley aplicable"],
                              targets=["jurisdicción", "juzgados", "aplicable", "applicable", "jurisdiction", "ley aplicable"],
                              limit=3)

    # Términos de uso / Terms of use
    hits = _push_keyword_hits(index_docs, hits, ql,
                              triggers=["términos de uso", "terminos de uso", "terms of use", "legal notice", "terms"],
                              targets=["términos", "terminos", "terms", "legal notice"],
                              limit=2)

    # Política de privacidad / Privacy policy
    hits = _push_keyword_hits(index_docs, hits, ql,
                              triggers=["política de privacidad", "politica de privacidad", "privacy policy", "data protection"],
                              targets=["política de privacidad", "privacy policy", "protección de datos", "data protection"],
                              limit=2)

    # Condiciones de reserva / Booking conditions
    hits = _push_keyword_hits(index_docs, hits, ql,
                              triggers=["condiciones de reserva", "booking conditions", "booking terms", "reserva"],
                              targets=["condiciones de reserva", "booking conditions", "booking terms"],
                              limit=2)

    # Contacto / Email
    hits = _push_keyword_hits(index_docs, hits, ql,
                              triggers=["contacto", "email", "contact", "correo"],
                              targets=["contacto", "email", "info@", "contact"],
                              limit=2)

    return hits[:top_k]

def format_context(scored):
    return "\n\n---\n".join([f"Fuente: {d['title']} (sim={s:.2f})\n{d['text']}" for s, d in scored])

# =========================
# Detección idioma + traducción simple
# =========================
def detect_lang(text: str) -> str:
    t = (text or "").lower()
    en_markers = [" how ", "how much", "price", "cost", "what", "when", "where", "is there", "do you"]
    if any(m in t for m in en_markers):
        return "en"
    es_markers = [" qué", "cuál", "cuanto", "cuánto", "precio", "fechas", "incluye", "dónde", "cuando", "política"]
    if any(m in t for m in es_markers):
        return "es"
    return "es"

def translate(text: str, target_lang: str) -> str:
    if not text:
        return ""
    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": f"Traduce al {target_lang} y devuelve solo el texto traducido."},
                {"role": "user", "content": text}
            ],
            temperature=0.0,
            max_tokens=300
        )
        return r.choices[0].message.content.strip()
    except Exception as e:
        print("Error traducción:", repr(e))
        return text

# =========================
# RAG principal
# =========================
FALLBACK_EMAIL_BY_BRAND = {
    "alandalus": "info@eltrenalandalus.com",
    "transcantabrico": "info@eltrentranscantabrico.com",
    "costaverde": "info@trencostaverdeexpress.com",
    "robla": "info@trenexpresodelarobla.com",
    None: "info@eltrenalandalus.com",
}

def rag_reply(user_text: str, brand: str = None, user_lang: str = None) -> str:
    user_lang = user_lang or detect_lang(user_text)
    query_for_kb = user_text if user_lang == "es" else translate(user_text, "es")

    hits = retrieve_context(query_for_kb, brand=brand, top_k=8, min_sim=0.50)
    context = format_context(hits) if hits else ""

    fallback_email = FALLBACK_EMAIL_BY_BRAND.get(brand, FALLBACK_EMAIL_BY_BRAND[None])

    system_rules = (
        f"Eres un asistente para trenes de lujo en España. "
        f"Responde EXCLUSIVAMENTE con la información de 'Base de conocimiento'. "
        f"No inventes ni generalices. Responde en idioma: {user_lang}. "
        f"Si la base no cubre la pregunta, responde EXACTAMENTE: "
        f"'No dispongo de esa información en la base de conocimiento. Por favor, escribe a {fallback_email}.' "
        f"Si hay contexto, extrae y lista los elementos relevantes respetando medidas, nombres y detalles."
    )

    messages = [{"role": "system", "content": system_rules}]
    if context:
        messages.append({"role": "system", "content": f"Base de conocimiento:\n{context}"})
    messages.append({"role": "user", "content": user_text})

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.0,
            max_tokens=900
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print("Error OpenAI:", repr(e))
        return "Lo siento, ha ocurrido un error al procesar tu consulta."

# =========================
# Utilidades Jivo
# =========================
def detect_brand_from_event(event: dict) -> str:
    blob = json.dumps(event, ensure_ascii=False).lower()
    if "eltrentranscantabrico.com" in blob: return "transcantabrico"
    if "eltrenalandalus.com"       in blob: return "alandalus"
    if "trencostaverdeexpress.com" in blob: return "costaverde"
    if "trenexpresodelarobla.com"  in blob: return "robla"
    return None

# =========================
# Rutas HTTP
# =========================
@app.route("/", methods=["GET"])
def index():
    return "Bot activo (RAG + Jivo Bot API)"

@app.route("/kb", methods=["GET"])
def kb_ls():
    brand = request.args.get("brand", "alandalus")
    folder = brand_folder(brand)
    try:
        files = sorted(os.listdir(folder))
        return jsonify({"brand": brand, "folder": folder, "files": files})
    except Exception as e:
        return jsonify({"brand": brand, "folder": folder, "error": str(e)}), 500

@app.route("/reindex", methods=["POST"])
def reindex():
    token = request.headers.get("X-Admin-Token")
    admin = os.environ.get("ADMIN_TOKEN")
    if admin and token != admin:
        return jsonify({"error": "unauthorized"}), 401

    payload = request.json or {}
    brand = payload.get("brand")

    reindexed = []

    def do_brand(b):
        INDEX_CACHE.pop(b or "general", None)
        docs = build_index_for(b)
        reindexed.append({"brand": b or "general", "chunks": len(docs)})

    if brand:
        do_brand(brand)
    else:
        do_brand(None)
        if os.path.isdir(KB_BASE):
            for name in os.listdir(KB_BASE):
                sub = os.path.join(KB_BASE, name)
                if os.path.isdir(sub):
                    do_brand(name)

    return jsonify({"status": "ok", "results": reindexed})

@app.route("/debug_search", methods=["POST"])
def debug_search():
    data = request.json or {}
    q = data.get("q", "")
    brand = data.get("brand")
    hits = retrieve_context(q, brand=brand, top_k=8, min_sim=0.50)
    return jsonify([{"sim": round(s, 3), "title": d["title"], "preview": d["text"][:220]} for s, d in hits])

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    user_message = (
        data.get("message")
        or data.get("text")
        or (data.get("payload") or {}).get("text")
        or (data.get("data") or {}).get("text")
        or ""
    ).strip()
    brand = data.get("brand")
    debug = bool(data.get("debug"))

    if not user_message:
        return jsonify({"reply": "¿Podrías reformular tu pregunta?"})

    user_lang = detect_lang(user_message)
    query_for_kb = user_message if user_lang == "es" else translate(user_message, "es")
    hits = retrieve_context(query_for_kb, brand=brand, top_k=8, min_sim=0.50)

    reply = rag_reply(user_message, brand=brand, user_lang=user_lang)
    resp = {"reply": reply}
    if debug:
        resp["debug"] = {
            "brand": brand or "general",
            "index_file": index_path_for(brand),
            "sources": [{"sim": round(s, 3), "title": d["title"], "preview": d["text"][:200]} for s, d in hits]
        }
    return jsonify(resp)

@app.route("/bot/<token>", methods=["POST"])
def bot_webhook(token):
    if TOKEN_MAP and token not in TOKEN_MAP:
        return jsonify({"error": "unauthorized"}), 401

    enabled_for_token = BOT_ENABLED_MAP.get(token, True)
    if not BOT_ENABLED or not enabled_for_token:
        return jsonify({"result": "bot_disabled"})

    event = request.json or {}
    ev_type = event.get("event")
    chat_id = event.get("chat_id")
    msg = (event.get("message") or {})
    text_in = (msg.get("text") or "").strip()

    if ev_type == "AGENT_JOINED":
        print("Agente se unió al chat", chat_id)
        return jsonify({"result": "ok"})
    if ev_type == "AGENT_UNAVAILABLE":
        bot_message(chat_id, "Ahora mismo no hay agentes disponibles. Déjanos tu email o teléfono y te contactaremos en breve.", token)
        return jsonify({"result": "ok"})
    if ev_type != "CLIENT_MESSAGE" or not chat_id or not text_in:
        return jsonify({"result": "ignored"})

    low = text_in.lower()
    if any(k in low for k in ["agente", "humano", "asesor", "speak to agent", "human", "operator"]):
        bot_message(chat_id, "Te paso con un agente humano ahora mismo.", token)
        invite_agent(chat_id, token)
        return jsonify({"result": "ok"})

    brand = detect_brand_from_event(event)
    user_lang = detect_lang(text_in)
    reply = rag_reply(text_in, brand=brand, user_lang=user_lang)

    if reply.strip().startswith("No dispongo de esa información"):
        bot_message(chat_id, "Te paso con un agente humano para que te ayude mejor.", token)
        invite_agent(chat_id, token)
        return jsonify({"result": "ok"})

    bot_message(chat_id, reply, token)
    return jsonify({"result": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
