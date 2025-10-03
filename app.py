from flask import Flask, request, jsonify
from openai import OpenAI
import os, glob, json, math

app = Flask(__name__)
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# -------- Configuración del RAG --------
EMBED_MODEL = "text-embedding-3-small"   # embeddings económicos y buenos
INDEX_PATH  = "kb_index.json"
KB_FOLDER   = "kb"
CHARS_PER_CHUNK = 800                    # tamaño de trozos (~caracteres)

# -------- Utilidades vectoriales (sin numpy) --------
def dot(a, b): return sum(x*y for x, y in zip(a, b))
def norm(v):   return math.sqrt(sum(x*x for x in v))
def cos_sim(a, b):
    na, nb = norm(a), norm(b)
    return 0.0 if na == 0 or nb == 0 else dot(a, b) / (na * nb)

# -------- Carga y troceado de la KB --------
def load_kb_files():
    docs = []
    for path in sorted(glob.glob(os.path.join(KB_FOLDER, "*.md"))):
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
        title = os.path.basename(path)
        # Partir en chunks de tamaño fijo
        for i in range(0, len(text), CHARS_PER_CHUNK):
            chunk = text[i:i+CHARS_PER_CHUNK].strip()
            if chunk:
                docs.append({"title": title, "text": chunk})
    return docs

# -------- Construir o cargar índice (con embeddings) --------
def build_index():
    docs = load_kb_files()
    if not docs:
        return []
    inputs = [d["text"] for d in docs]
    emb = client.embeddings.create(model=EMBED_MODEL, input=inputs)
    for d, e in zip(docs, emb.data):
        d["embedding"] = e.embedding
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(docs, f, ensure_ascii=False)
    return docs

def load_or_build_index():
    if os.path.exists(INDEX_PATH):
        try:
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return build_index()

KB_INDEX = load_or_build_index()

# -------- NUEVA recuperación con fallback --------
def retrieve_context(query, top_k=8, min_sim=0.50):
    """
    Recupera hasta top_k trozos relevantes:
    - Usa similitud coseno sobre embeddings.
    - Si no hay nada por encima del umbral, devuelve al menos los 2 mejores.
    - Si la query habla de 'incluye', inyecta 1–2 trozos que contengan esa palabra.
    """
    if not KB_INDEX:
        return []

    # Embedding de la query
    q_emb = client.embeddings.create(model=EMBED_MODEL, input=[query]).data[0].embedding

    # Puntuar por similitud
    scored = []
    for d in KB_INDEX:
        s = cos_sim(q_emb, d["embedding"])
        scored.append((s, d))
    scored.sort(key=lambda x: x[0], reverse=True)

    # Candidatos por umbral
    hits = [(s, d) for s, d in scored[:top_k] if s >= min_sim]

    # Fallback 1: si no hay hits, quedarnos con los 2 mejores
    if not hits and scored:
        hits = scored[:2]

    # Fallback 2: si la pregunta contiene "incluye", empujar trozos por palabra clave
    ql = (query or "").lower()
    if ("incluye" in ql) or ("qué incluye" in ql) or ("que incluye" in ql):
        keyword_hits = [(0.99, d) for d in KB_INDEX
                        if ("incluye" in d["title"].lower() or "incluye" in d["text"].lower())]
        if keyword_hits:
            keyword_hits = keyword_hits[:2]
            # Evitar duplicados
            seen = {id(d) for _, d in hits}
            keyword_hits = [(s, d) for s, d in keyword_hits if id(d) not in seen]
            hits = keyword_hits + hits

    return hits[:top_k]

def format_context(scored):
    # Incluimos título y una “pista” de similitud para depurar
    parts = []
    for s, d in scored:
        parts.append(f"Fuente: {d['title']} (sim={s:.2f})\n{d['text']}")
    return "\n\n---\n".join(parts)

# -------- Rutas --------
@app.route("/", methods=["GET"])
def index():
    return "Bot activo (RAG)"

@app.route("/reindex", methods=["POST"])
def reindex():
    """
    Reconstruye el índice sin redeploy.
    Protege con un token opcional: añade en Render la variable ADMIN_TOKEN
    y envía el header: X-Admin-Token: <tu_token>
    """
    token = request.headers.get("X-Admin-Token")
    admin = os.environ.get("ADMIN_TOKEN")
    if admin and token != admin:
        return jsonify({"error": "unauthorized"}), 401
    global KB_INDEX
    KB_INDEX = build_index()
    return jsonify({"status": "ok", "chunks": len(KB_INDEX)})

@app.route("/debug_search", methods=["POST"])
def debug_search():
    """
    Diagnóstico: ver qué trozos devuelve el recuperador.
    Body JSON: {"q": "tu pregunta"}
    """
    q = (request.json or {}).get("q", "")
    hits = retrieve_context(q, top_k=8, min_sim=0.50)
    return jsonify([
        {"sim": round(s, 3), "title": d["title"], "preview": d["text"][:220]} for s, d in hits
    ])

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

    if not user_message:
        return jsonify({"reply": "¿Podrías reformular tu pregunta?"})

    if not KB_INDEX:
        return jsonify({"reply": "Base de conocimiento vacía. Añade .md en /kb y vuelve a desplegar o llama /reindex."})

    # Recuperar contexto con fallback
    hits = retrieve_context(user_message, top_k=8, min_sim=0.50)
    context = format_context(hits) if hits else ""

    # Prompt estricto: SOLO KB; si no hay datos, mensaje fijo
    system_rules = (
        "Eres un asistente para El Tren Transcantábrico. "
        "Responde EXCLUSIVAMENTE con la información proporcionada en 'Base de conocimiento'. "
        "No inventes ni generalices. Si la base no cubre la pregunta, responde EXACTAMENTE: "
        "'No dispongo de esa información en la base de conocimiento. Por favor, escribe a info@eltrentranscantabrico.com.' "
        "Si hay contexto, extrae y lista los elementos relevantes respetando medidas, nombres y detalles."
    )

    messages = [{"role": "system", "content": system_rules}]
    if context:
        messages.append({"role": "system", "content": f"Base de conocimiento:\n{context}"})
    messages.append({"role": "user", "content": user_message})

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.0,     # modo extractivo
            max_tokens=900
        )
        reply = resp.choices[0].message.content.strip()
    except Exception as e:
        print("Error OpenAI:", repr(e))
        reply = "Lo siento, ha ocurrido un error al procesar tu consulta."

    return jsonify({"reply": reply})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
