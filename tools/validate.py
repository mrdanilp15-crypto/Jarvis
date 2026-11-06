#!/usr/bin/env python3
"""Validiert alle maschinenlesbaren Artefakte des Repositories.

- JSON-Schemas (Draft 2020-12) und alle Beispiel-Payloads in schemas/examples
- Persona-Konfigurationen und Plugin-Manifeste gegen ihre Schemas
- Syntax aller YAML-Dateien (inkl. Home-Assistant-Tags wie !secret)
- Referenzen in api/openapi.yaml und Verdrahtung der Node-RED-Flows

Benötigt: jsonschema, pyyaml.   Aufruf: python tools/validate.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"
errors: list[str] = []


def fail(msg: str) -> None:
    errors.append(msg)
    print(f"  ✖ {msg}")


def ok(msg: str) -> None:
    print(f"  ✔ {msg}")


class HomeAssistantLoader(yaml.SafeLoader):
    """Akzeptiert HA-spezifische Tags (!secret, !include, …) als Platzhalter."""


for tag in ("!secret", "!include", "!include_dir_named", "!include_dir_list", "!include_dir_merge_named",
            "!include_dir_merge_list", "!env_var", "!input"):
    HomeAssistantLoader.add_constructor(tag, lambda loader, node: f"<{node.tag} {node.value}>")


def load_schemas() -> tuple[dict[str, dict], Registry]:
    schemas, resources = {}, []
    for path in sorted(SCHEMAS.glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        try:
            Draft202012Validator.check_schema(schema)
            ok(f"Schema {path.name}")
        except Exception as exc:  # noqa: BLE001
            fail(f"Schema {path.name}: {exc}")
        schemas[path.name.removesuffix(".schema.json")] = schema
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return schemas, Registry().with_resources(resources)


def validate_instance(name: str, instance: object, label: str, schemas: dict, registry: Registry) -> None:
    validator = Draft202012Validator(schemas[name], registry=registry, format_checker=FormatChecker())
    problems = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    if problems:
        for p in problems:
            fail(f"{label}: /{'/'.join(map(str, p.absolute_path))}: {p.message}")
    else:
        ok(f"{label} ✓ {name}")


def check_node_red(path: Path) -> None:
    nodes = json.loads(path.read_text(encoding="utf-8"))
    ids = {n["id"] for n in nodes}
    tabs = {n["id"] for n in nodes if n["type"] == "tab"}
    bad = []
    for node in nodes:
        if "z" in node and node["z"] not in tabs:
            bad.append(f"{node['id']}: unbekannter Tab {node['z']}")
        for key in ("broker", "tls"):
            if key == "broker" and node["type"] == "mqtt-broker":
                continue  # dort ist "broker" der Hostname
            if node.get(key) and node[key] not in ids:
                bad.append(f"{node['id']}: unbekannte Konfiguration {node[key]}")
        for outputs in node.get("wires", []):
            bad.extend(f"{node['id']}: Draht zu unbekanntem Knoten {t}" for t in outputs if t not in ids)
        if node["type"] == "function" and len(node.get("wires", [])) != node.get("outputs", 1):
            bad.append(f"{node['id']}: Anzahl Ausgänge passt nicht zu wires")
    for msg in bad:
        fail(f"{path.relative_to(ROOT)}: {msg}")
    if not bad:
        ok(f"{path.relative_to(ROOT)} ({len(nodes)} Knoten, Verdrahtung konsistent)")


def check_openapi(path: Path) -> None:
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    refs: list[str] = []

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            if isinstance(obj.get("$ref"), str):
                refs.append(obj["$ref"])
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(spec)
    missing = []
    for ref in refs:
        target, _, pointer = ref.partition("#")
        doc = spec if not target else json.loads((path.parent / target).read_text(encoding="utf-8"))
        node = doc
        for part in [p for p in pointer.split("/") if p]:
            node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            missing.append(ref)
    for ref in missing:
        fail(f"openapi.yaml: $ref nicht auflösbar: {ref}")
    if not missing:
        ok(f"api/openapi.yaml ({len(spec['paths'])} Pfade, {len(refs)} Referenzen auflösbar)")
    try:
        from openapi_spec_validator import validate  # optional

        validate(spec, base_uri=path.resolve().as_uri())
        ok("api/openapi.yaml gültig laut openapi-spec-validator")
    except ImportError:
        print("  · openapi-spec-validator nicht installiert – Strukturprüfung übersprungen")
    except Exception as exc:  # noqa: BLE001
        fail(f"openapi.yaml: {exc}")


def main() -> int:
    print("JSON-Schemas")
    schemas, registry = load_schemas()

    print("Beispiel-Payloads")
    for path in sorted((SCHEMAS / "examples").glob("*.json")):
        name = path.name.split(".")[0]
        validate_instance(name, json.loads(path.read_text(encoding="utf-8")), f"examples/{path.name}", schemas, registry)

    print("Konfiguration & Manifeste")
    for path in sorted((ROOT / "config").glob("persona.*.yaml")):
        validate_instance("persona", yaml.safe_load(path.read_text(encoding="utf-8")), f"config/{path.name}",
                          schemas, registry)
    for path in sorted(ROOT.glob("reference/**/plugin.json")):
        validate_instance("plugin-manifest", json.loads(path.read_text(encoding="utf-8")),
                          str(path.relative_to(ROOT)), schemas, registry)

    print("YAML-Syntax")
    for path in sorted(p for p in ROOT.rglob("*.y*ml") if ".git" not in p.parts and "node_modules" not in p.parts):
        try:
            yaml.load(path.read_text(encoding="utf-8"), Loader=HomeAssistantLoader)  # noqa: S506 – SafeLoader-Basis
            ok(str(path.relative_to(ROOT)))
        except yaml.YAMLError as exc:
            fail(f"{path.relative_to(ROOT)}: {exc}")

    print("Integrationen")
    for path in sorted((ROOT / "integrations").rglob("*.json")):
        if "node-red" in path.parts:
            check_node_red(path)
        else:
            json.loads(path.read_text(encoding="utf-8"))
            ok(str(path.relative_to(ROOT)))
    check_openapi(ROOT / "api" / "openapi.yaml")

    print(f"\n{'FEHLER: ' + str(len(errors)) if errors else 'Alles gültig.'}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
