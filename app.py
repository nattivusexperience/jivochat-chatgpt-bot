from flask import Flask, request, jsonify
import openai
import os

app = Flask(__name__)
openai.api_key = os.environ.get("OPENAI_API_KEY")

@app.route("/", methods=["GET"])
def index():
    return "Bot activo"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    user_message = data.get("message") or data.get("text") or ""

    try:
        # Si usas el SDK antiguo:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Eres un asistente para El Tren Transcantábrico. Responde con claridad y amabilidad."},
                {"role": "user", "content": user_message}
            ],
            max_tokens=500,
            temperature=0.7
        )
        reply = response.choices[0].message.content.strip()
    except Exception as e:
        print("Error al llamar a OpenAI:", e)
        reply = "Lo siento, ha ocurrido un error al procesar tu consulta."

    return jsonify({"reply": reply})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
