from pyngrok import ngrok
from flask import Flask
import time

# Set your ngrok authtoken
ngrok.set_auth_token("2nkLwKqo0pBsIaw9qCFplVvlCgf_5jFV314NWtQ31SMkJjPFv")

# Step 3: Create a simple Flask application
app = Flask(_name_)

@app.route("/")
def home():
    return "Hello, this is my ngrok server running in Google Colab!"

# Step 4: Start the Flask app in a separate thread
import threading

def run_flask():
    app.run(port=5000)

flask_thread = threading.Thread(target=run_flask)
flask_thread.start()

# Step 5: Start ngrok tunnel
public_url = ngrok.connect(5000)  # Match the port your Flask app is running on
print(f" * ngrok tunnel \"{public_url}\" -> \"http://127.0.0.1:5000\"")

# Step 6: Keep the server running
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("Shutting down...")
    ngrok.disconnect(public_url)  # Disconnect ngrok tunnel