"""JARVIS Referenz-Kern.

Die Module sind so geschnitten, dass die sicherheitskritische Kernlogik (Policy, Tool-Validierung,
Bestätigungen, Taint-Tracking, Automations-Bedingungen, Memory-Ranking) ohne externe Dienste testbar ist.
Anbindungen an LLMs, Home Assistant, MQTT, Redis und Postgres importieren ihre Bibliotheken erst bei Bedarf.
"""

__version__ = "0.1.0"
