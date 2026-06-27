"""Singleton SlowAPI limiter partagé entre main.py et les routers.

Ce module évite l'import circulaire : main.py importe les routers,
donc si les routers importaient limiter depuis main.py on aurait un cycle.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
