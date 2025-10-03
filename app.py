from flask import Flask, request, jsonify
from openai import OpenAI
import os, glob, json, math

app = Flask(__name__)
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

EMBED_MODEL = "text-embedding-3-small"
INDEX_PATH  = "kb_index.json"
KB_FOLDER   = "kb"
CHARS_PER_CHUNK = 800

def dot(a,b): return sum(x*y for x,y in zip(a,b))
def norm(v): return math.sqrt(sum(x*x for x in v))
def cos_sim(a,b):
    na, nb = norm(a), norm(b)
    return 0.0 if na == 0 or nb == 0 else dot(a,b)/(na*nb)

def load_kb_files():
    docs = []
    for path in sorted(glob.glob(os.path.join(KB_FOLDER, "*.md"))):
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
        title = os.path.basename(path)
        for i in range(0, len(text), CHARS_PER_CHUNK):
            chunk = text[i:i+CHARS_PER_CHUNK].strip()
            if chunk:
                docs.append({"title": title, "text": chunk})
    return docs

def build_index():
    docs = load_kb_files()
    if not docs: return []
    inputs = [d["text"] for d in docs]
    emb = client.embeddings.create(model=EMBED_MODEL, input=inputs)
    for d, e in zip(docs, emb.data):
        d["embedding"] = e.embedding
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(docs, f, ensure_ascii=False)
    return docs

def load_or_build_index():
    if os.path.exists(INDEX_PATH):
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return build_index()

KB_INDEX = load_or_build_index()

def retrieve_context(query, top_k=3, min_sim=0.70):
    if not KB_INDEX: return []
    q_emb = client.embeddings.create(model=EMBED_MODEL, input=[query]).data[0].embedding
    scored = [(cos_sim(q_emb, d["embedding"]), d) for d in KB_INDEX]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [(s,d) for s,d in scored[:top_k] if s >= min_sim]

def format_context(scored):
    return "\n\n---\n".join([f"Fuente: {d['title']} (sim={s:.2f})\n{d['text']}" for s,d in scored])

@app.route("/", methods=["GET"])
def index():
    return "Bot activo (RAG)"

@app.route("/reindex", methods=["POST"])
def reindex():
    # Opcional: protege con token
    token = request.headers.get("X-Admin-Token")
    admin = os.environ.get("ADMIN_TOKEN")
    if admin and token != admin:
        return jsonify({"error":"unauthorized"}), 401
    global KB_INDEX
    KB_INDEX = build_index()
    return jsonify({"status":"ok", "chunks": len(KB_INDEX)})

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    user_message = (
        data.get("message")
        or data.get("text")
        or (data.get("payload") or {}).get("text")
        or (data.get("data") or {}).get("text")
        or ""
    )

    if not KB_INDEX:
        return jsonify({"reply":"Base de conocimiento vacía. Añade .md en /kb y vuelve a desplegar o llama /reindex."})

    hits = retrieve_context(user_message, top_k=3, min_sim=0.70)
    context = format_context(hits) if hits else ""

    system_rules = (
        "Eres un asistente para El Tren Transcantábrico. "
        "Responde SOLO con la información de la 'Base de conocimiento' adjunta. "
        "Si la base no cubre la pregunta, dilo con claridad y sugiere escribir a info@eltrentranscantabrico.com. "
        "No inventes datos."
    )

    messages = [{"role":"system","content": system_rules}]
    if context:
        messages.append({"role":"system","content": f"Base de conocimiento:\n{context}"})
    messages.append({"role":"user","content": user_message})

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.2,
            max_tokens=500
        )
        reply = resp.choices[0].message.content.strip()
    except Exception as e:
        print("Error OpenAI:", repr(e))
        reply = "Lo siento, ha ocurrido un error al procesar tu consulta."

    return jsonify({"reply": reply})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
