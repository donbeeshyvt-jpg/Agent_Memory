"""Agent-facing tool schemas."""

from __future__ import annotations


def memory_tool_schema() -> dict:
    """Unified memory tool schema for add/replace/remove/get."""

    return {
        "name": "memory",
        "description": "Unified memory operation tool for Obsidian-backed memory core.",
        "input_schema": {
            "type": "object",
            "required": ["action", "path"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "replace", "remove", "get"],
                    "description": "Memory operation action.",
                },
                "path": {
                    "type": "string",
                    "description": "Vault-relative markdown path.",
                },
                "content": {
                    "type": "string",
                    "description": "Body content used by add/replace actions.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional reason for replace/remove.",
                    "default": "",
                },
                "agent": {
                    "type": "string",
                    "description": "Writer agent identity.",
                    "default": "agent-memory-core",
                },
                "source": {
                    "type": "string",
                    "enum": ["user", "agent", "flush", "mirror", "promotion"],
                    "default": "agent",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional frontmatter tags.",
                },
                "extras": {
                    "type": "object",
                    "description": "Optional frontmatter extras map.",
                },
            },
            "allOf": [
                {
                    "if": {"properties": {"action": {"const": "add"}}},
                    "then": {"required": ["content"]},
                },
                {
                    "if": {"properties": {"action": {"const": "replace"}}},
                    "then": {"required": ["content"]},
                },
            ],
        },
    }


def memory_search_schema() -> dict:
    """Retrieval tool schema backed by sqlite + FTS."""

    return {
        "name": "memory_search",
        "description": "Search Obsidian-backed memory index with path-scoped retrieval.",
        "input_schema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "Natural language retrieval query."},
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results to return.",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 50,
                },
                "include_archived": {
                    "type": "boolean",
                    "description": "Whether archived notes should be included.",
                    "default": False,
                },
                "auto_reindex": {
                    "type": "boolean",
                    "description": "Run incremental reindex before searching.",
                    "default": True,
                },
            },
        },
    }


def memory_get_schema() -> dict:
    """Direct memory fetch schema with citation metadata."""

    return {
        "name": "memory_get",
        "description": "Get one memory note by vault-relative path.",
        "input_schema": {
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {"type": "string", "description": "Vault-relative markdown path."},
            },
        },
    }


def tool_schema_bundle() -> list[dict]:
    """Return all exposed memory tool schemas."""

    return [memory_tool_schema(), memory_search_schema(), memory_get_schema()]
