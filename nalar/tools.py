"""A validated registry of tools shared by the CLI, TUI and SDK."""

import re
from fnmatch import fnmatch

from .workspace import ToolError

NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")

# Short recovery guidance for common tool failures. Static text only.
HINTS = {
    "file_unavailable": "The path is not a readable text file.",
    "not_text": "The file is binary or not valid UTF-8.",
    "no_match": "The exact text was not found; re-read the file and use an exact span.",
    "ambiguous_match": "The exact text occurs more than once; include more surrounding text.",
    "empty_match": "The before text must not be empty.",
    "invalid_range": "start/end are one-based and a range is at most 120 lines.",
    "range_too_large": "Read fewer lines at once.",
    "invalid_file_content": "Edited files must be UTF-8 text under the size limit.",
    "invalid_file": ("The path must be a regular file with one link and no "
                     "symlinks."),
    "empty_file": "Pass non-empty content when creating a file.",
    "invalid_limit": "The limit must be between 1 and 20.",
    "invalid_uri": "Use a listed resource URI or a matching template.",
    "invalid_patch": ("Provide a unified diff with correct hunk counts, one section per "
                      "existing file and at least one hunk."),
    "patch_too_large": "Split the change into patches smaller than 200 KB.",
    "patch_conflict": "The patch context did not match; re-read the file and regenerate it.",
    "patch_ambiguous": "The patch context matches more than once; include more context lines.",
    "already_deleted": "The file is already staged for deletion.",
    "path_staged_deleted": ("The target is staged for deletion; restore it or choose "
                            "another path."),
    "repeated_call": "This exact request already failed; change it or inspect state first.",
    "invalid_plan": ("Send 1-20 unique single-line steps with status pending, "
                     "in_progress or completed, and at most one in progress."),
    "edit_limit": "Too many files are staged; finish or split the task.",
    "invalid_query": ("Use a non-empty single-line query (up to 120 characters in a "
                      "workspace, 200 in a repository)."),
    "invalid_sha": "Use a 7-64 character lowercase hexadecimal commit id.",
    "invalid_prefix": "Use a repository-relative prefix.",
    "invalid_offset": "The offset must be a non-negative integer.",
    "invalid_path": "Use a safe repository-relative path.",
    "invalid_url": "Use an https URL (or loopback http).",
    "not_found": "The name does not exist in the workspace.",
    "invalid_name": ("Use a name starting with a letter or digit, up to 80 "
                     "characters."),
    "already_exists": "That name already exists; choose another.",
    "file_too_large": "Workspace files are limited to 16 KB.",
    "file_changed": "The file changed while it was open; re-read it before editing.",
    "invalid_utf8": "The file is not valid UTF-8.",
    "tool_failed": "The tool failed unexpectedly.",
}


class ToolBelt:
    def __init__(self):
        self._specs = []
        self._handlers = {}
        self._schemas = {}
        self._ask = ()

    def add(self, spec, handler):
        if not callable(handler):
            raise ValueError("tool handler must be callable")
        try:
            function = spec["function"]
            name = function["name"]
            description = function.get("description", "")
            parameters = function["parameters"]
        except (KeyError, TypeError):
            raise ValueError("tool spec must use the function shape") from None
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError("tool names use letters, digits, _ and - only")
        if name in self._handlers:
            raise ValueError(f"duplicate tool name: {name}")
        if not isinstance(description, str) or len(description) > 1024:
            raise ValueError("tool description is invalid")
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise ValueError("tool parameters must be an object schema")
        self._specs.append({"type": "function", "function": {
            "name": name, "description": description, "parameters": parameters}})
        self._handlers[name] = handler
        self._schemas[name] = parameters
        return self

    def requires_approval(self, name):
        return any(fnmatch(name, pattern) for pattern in self._ask)

    def apply_policy(self, allow=None, deny=(), ask=()):
        """Restrict which tools are exposed; deny wins over allow."""
        def permitted(name):
            if any(fnmatch(name, pattern) for pattern in deny):
                return False
            return allow is None or any(fnmatch(name, pattern) for pattern in allow)

        self._specs = [spec for spec in self._specs
                       if permitted(spec["function"]["name"])]
        self._handlers = {name: handler for name, handler in self._handlers.items()
                          if permitted(name)}
        self._ask = tuple(ask)
        return self

    @property
    def specs(self):
        return [dict(spec) for spec in self._specs]

    @property
    def names(self):
        return set(self._handlers)

    def _schema_hint(self, name):
        parameters = self._schemas.get(name) or {}
        required = [item for item in (parameters.get("required") or [])
                    if isinstance(item, str)][:12]
        keys = [item for item in (parameters.get("properties") or {})
                if isinstance(item, str)][:12]
        parts = []
        if required:
            parts.append("required: " + ", ".join(required))
        if keys:
            parts.append("keys: " + ", ".join(keys))
        return "; ".join(parts)[:300]

    def call(self, name, args):
        handler = self._handlers.get(name)
        if handler is None:
            raise ToolError("unknown_tool")
        try:
            return handler(args)
        except ToolError as exc:
            code = str(exc)
            if not getattr(exc, "detail", None):
                hint = self._schema_hint(name) if code == "invalid_arguments" else HINTS.get(code)
                if hint:
                    raise ToolError(code, nonfatal=getattr(exc, "nonfatal", False),
                                    detail=hint) from None
            raise
        except Exception:
            raise ToolError("tool_failed") from None
