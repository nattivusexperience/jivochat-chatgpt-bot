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
# En tu caso: {"jivo_tt_9A7xQ2LmC4fD":"trenes"}
try:
    TOKEN_MAP = json.loads(os.getenv("JIVO_TOKEN_MAP", "{}"))
except Exception:
    TOKEN_MAP = {}

# provider_id por token (cuando Jivo te lo envíe)
# Ej: {"jivo_tt_9A7xQ2LmC4fD":"PROV123"}
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
    """
    Devuelve la carpeta de KB para una marca:
    - Si existe kb/<brand>/, úsala.
    - Si no, usa kb/ raíz (modo actual).
    """
    if brand:
        candidate = os.path.join(KB_BASE, brand)
        if os.path.isdir(candidate):
            return candidate
    return KB_BASE

def index_path_for(brand: str) -> str:
    """
    Fichero de índice por marca. Si no hay marca, usa el índice general.
    """
    safe = (brand or "general").replace("/", "_")
    return f"kb_index_{safe}.json"

def load_kb_files(kb_folder: str):
    docs = []

    # 1) .md (como ya tenías)
    for path in sorted(glob.glob(os.path.join(kb_folder, "*.md"))):
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
        title = os.path.basename(path)
        for i in range(0, len(text), CHARS_PER_CHUNK):
            chunk = text[i:i+CHARS_PER_CHUNK].strip()
            if chunk:
                docs.append({"title": title, "text": chunk})

    # 2) .jsonl (NUEVO)
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
                title = f"{os.path.basename(path)}::{rec.get('id') or rec.get('title') or 'chunk'}"
                if content:
                    # Normalización suave: emails con espacios tipo "info@ eltrenalandalus.com"
                    content = content.replace(" @", "@").replace("@ ", "@")
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
# Recuperación con fallback
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

    hits = [(s, d) for s, d in scored[:top_k] if s >= min_sim]
    if not hits and scored:
        hits = scored[:2]  # siempre algo

    ql = (query or "").lower()
    # empujar "incluye"
    if ("incluye" in ql) or ("qué incluye" in ql) or ("que incluye" in ql):
        k_hits = keyword_hits(index_docs, ["incluye"], limit=2)
        seen = {id(d) for _, d in hits}
        k_hits = [(s, d) for s, d in k_hits if id(d) not in seen]
        hits = k_hits + hits

    # empujar "precio/coste/2026"
    if any(k in ql for k in ["precio", "precios", "coste", "price", "cost", "how much", "cuánto", "cuanto", "2026"]):
        k_hits = keyword_hits(index_docs, ["precio", "precios", "2026", "€"], limit=3)
        seen = {id(d) for _, d in hits}
        k_hits = [(s, d) for s, d in k_hits if id(d) not in seen]
        hits = k_hits + hits

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
    # por defecto español
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
    None: "info@eltrenalandalus.com",   # default razonable
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
    ...


    messages = [{"role": "system", "content": system_rules}]
    if context:
        messages.append({"role": "system", "content": f"Base de conocimiento:\n{context}"})
    messages.append({"role": "user", "content": user_text})  # mantenemos el idioma del usuario

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
    """
    Detecta la marca (tren) por el dominio del canal/datos del evento.
    Si no encuentra nada, devuelve None y usaremos la KB general (kb/).
    """
    blob = json.dumps(event, ensure_ascii=False).lower()
    if "eltrentranscantabrico.com" in blob: return "transcantabrico"
    if "eltrenalandalus.com"       in blob: return "alandalus"
    if "trencostaverdeexpress.com" in blob: return "costaverde"
    if "trenexpresodelarobla.com"  in blob: return "robla"
    return None  # KB general (kb/)

# =========================
# Rutas HTTP
# =========================
@app.route("/", methods=["GET"])
def index():
    return "Bot activo (RAG + Jivo Bot API)"

@app.route("/reindex", methods=["POST"])
def reindex():
    """
    Reconstruye índices. Opciones:
    - Sin body: reindexa todas las marcas detectables (kb/ y subcarpetas directas).
    - Body JSON: {"brand":"transcantabrico"} reindexa solo esa marca.
    Protege opcionalmente con ADMIN_TOKEN en header 'X-Admin-Token'.
    """
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
        # reindex kb/ general
        do_brand(None)
        # reindex subcarpetas directas como marcas
        if os.path.isdir(KB_BASE):
            for name in os.listdir(KB_BASE):
                sub = os.path.join(KB_BASE, name)
                if os.path.isdir(sub):
                    do_brand(name)

    return jsonify({"status": "ok", "results": reindexed})

@app.route("/debug_search", methods=["POST"])
def debug_search():
    """
    Diagnóstico: ver qué trozos devuelve el recuperador.
    Body JSON: {"q": "...", "brand": "transcantabrico"}
    """
    data = request.json or {}
    q = data.get("q", "")
    brand = data.get("brand")
    hits = retrieve_context(q, brand=brand, top_k=8, min_sim=0.50)
    return jsonify([{"sim": round(s, 3), "title": d["title"], "preview": d["text"][:220]} for s, d in hits])

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Endpoint genérico (curl/pruebas). Usa la KB general (kb/) salvo que envíes {"brand":"..."}.
    """
    data = request.json or {}
    user_message = (
        data.get("message")
        or data.get("text")
        or (data.get("payload") or {}).get("text")
        or (data.get("data") or {}).get("text")
        or ""
    ).strip()
    brand = data.get("brand")  # opcional en pruebas

    if not user_message:
        return jsonify({"reply": "¿Podrías reformular tu pregunta?"})

    # responder en el idioma del usuario (detección simple)
    user_lang = detect_lang(user_message)
    reply = rag_reply(user_message, brand=brand, user_lang=user_lang)
    return jsonify({"reply": reply})

@app.route("/bot/<token>", methods=["POST"])
def bot_webhook(token):
    """
    Endpoint oficial para Jivo Bot API.
    - Valida token.
    - Detecta marca por dominio del evento.
    - Responde con KB y hace handoff a agente si procede.
    """
    # Validación de token
    if TOKEN_MAP and token not in TOKEN_MAP:
        return jsonify({"error": "unauthorized"}), 401

    # Toggle encendido/apagado
    enabled_for_token = BOT_ENABLED_MAP.get(token, True)
    if not BOT_ENABLED or not enabled_for_token:
        return jsonify({"result": "bot_disabled"})

    event = request.json or {}
    ev_type = event.get("event")
    chat_id = event.get("chat_id")
    msg = (event.get("message") or {})
    text_in = (msg.get("text") or "").strip()

    # Eventos del sistema
    if ev_type == "AGENT_JOINED":
        print("Agente se unió al chat", chat_id)
        return jsonify({"result": "ok"})
    if ev_type == "AGENT_UNAVAILABLE":
        bot_message(chat_id, "Ahora mismo no hay agentes disponibles. Déjanos tu email o teléfono y te contactaremos en breve.", token)
        return jsonify({"result": "ok"})
    if ev_type != "CLIENT_MESSAGE" or not chat_id or not text_in:
        return jsonify({"result": "ignored"})

    # Handoff por petición explícita
    low = text_in.lower()
    if any(k in low for k in ["agente", "humano", "asesor", "speak to agent", "human", "operator"]):
        bot_message(chat_id, "Te paso con un agente humano ahora mismo.", token)
        invite_agent(chat_id, token)
        return jsonify({"result": "ok"})

    # Detectar marca según dominio del canal
    brand = detect_brand_from_event(event)

    # Idioma del usuario
    user_lang = detect_lang(text_in)

    # Respuesta con RAG (KB de la marca si existe carpeta kb/<brand>, si no kb/ general)
    reply = rag_reply(text_in, brand=brand, user_lang=user_lang)

    # Handoff si no hay información en KB
    if reply.strip().startswith("No dispongo de esa información"):
        bot_message(chat_id, "Te paso con un agente humano para que te ayude mejor.", token)
        invite_agent(chat_id, token)
        return jsonify({"result": "ok"})

    bot_message(chat_id, reply, token)
    return jsonify({"result": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

@app.route("/kb_ls", methods=["GET"])
def kb_ls():
    brand = request.args.get("brand", "alandalus")
    folder = brand_folder(brand)
    try:
        files = sorted(os.listdir(folder))
    except Exception as e:
        return jsonify({"brand": brand, "folder": folder, "error": str(e)}), 500
    return jsonify({"brand": brand, "folder": folder, "files": files})
