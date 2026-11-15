"""Errores del contexto access."""
from __future__ import annotations


class AccessDenied(Exception):
    """El dispositivo no puede enviar datos o recibir comandos. El texto es el motivo (se informa al nodo)."""


class InvalidMacAddress(ValueError):
    pass
