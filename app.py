from flask import Flask, request, jsonify
from openai import OpenAI
import os

app = Flask(__name__)
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

@app.route("/", methods=["GET"])
def index():
    return "Bot activo"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    user_message = data.get("message") or data.get("text") or ""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",  # modelo actual, rápido y barato
            messages=[
                {"role": "system", "content": "Eres un asistente para El Tren Transcantábrico. Responde de forma clara y amable."},
                {"role": "user", "content": user_message}
            ],
            max_tokens=500,
            temperature=0.7,
        )
        reply = resp.choices[0].message.content.strip()
    except Exception as e:
        # Log detallado a Render Logs
        print("Error OpenAI:", repr(e))
        reply = "Lo siento, ha ocurrido un error al procesar tu consulta."

    return jsonify({"reply": reply})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

