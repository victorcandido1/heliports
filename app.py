"""Servidor estático para o mapa de helipontos — deploy Railway."""
import os

from flask import Flask, send_from_directory

app = Flask(__name__, static_folder=".", static_url_path="")

ROOT = os.path.dirname(os.path.abspath(__file__))


@app.route("/")
def index():
    """Redireciona para o mapa."""
    return send_from_directory(ROOT, "mapa_helipontos_sp.html")


@app.route("/<path:path>")
def serve(path):
    """Serve arquivos estáticos."""
    return send_from_directory(ROOT, path)
