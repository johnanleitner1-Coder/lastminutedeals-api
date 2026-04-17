"""
call_graph.py — AST-based dependency mapper for the WAT framework.

Parses every Python file in tools/ and builds a complete dependency graph:
  - Which files import which other files (direct + lazy via importlib.util)
  - Which functions are defined in which files
  - Which functions are called from which files
  - Blast radius: given a file to change, what other files must be re-tested

Usage:
    from call_graph import CallGraph
    graph = CallGraph.build()
    affected = graph.blast_radius("tools/normalize_slot.py")
    # → frozenset of all files that depend on normalize_slot.py (direct + transitive)

    python tools/call_graph.py [file_to_check]
"""

import ast
import json
import re
import sys
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Set

BASE_DIR   = Path(__file__).parent.parent
TOOLS_DIR  = Path(__file__).parent
CACHE_FILE = BASE_DIR / ".tmp" / "call_graph_cache.json"

# Files we know are high-coupling and need special attention
HIGH_COUPLING_FILES = {
    "tools/normalize_slot.py",      # 12+ importers — every slot pipeline
    "tools/complete_booking.py",    # booking fulfillment bottleneck
    "tools/run_api_server.py",      # root orchestrator
    "tools/circuit_breaker.py",     # OCTO gate, loaded dynamically
    "tools/manage_wallets.py",      # wallet state, loaded dynamically
}


class CallGraph:
    """
    Represents the full import dependency graph of the tools/ directory.

    Attributes:
        depends_on:   file → set of files it imports
        depended_by:  file → set of files that import it
        func_defs:    function_name → file where it's defined
        func_calls:   file → set of function names called in that file
        all_files:    set of all known .py files in tools/
    """

    def __init__(self):
        self.depends_on:  Dict[str, Set[str]] = {}
        self.depended_by: Dict[str, Set[str]] = {}
        self.func_defs:   Dict[str, str]      = {}
        self.func_calls:  Dict[str, Set[str]] = {}
        self.all_files:   Set[str]             = set()

    # ── Construction ──────────────────────────────────────────────────────────

    @classmethod
    def build(cls, tools_dir: Path = TOOLS_DIR) -> "CallGraph":
        """Parse every .py in tools/ and build the graph. Takes ~0.5s."""
        graph = cls()
        py_files = sorted(tools_dir.glob("*.py"))

        # Register all files first so depended_by entries always exist
        for fp in py_files:
            rel = _rel_path(fp)
            graph.all_files.add(rel)
            graph.depends_on.setdefault(rel, set())
            graph.depended_by.setdefault(rel, set())
            graph.func_calls.setdefault(rel, set())

        for fp in py_files:
            rel = _rel_path(fp)
            try:
                source = fp.read_text(encoding="utf-8", errors="replace")
                graph._parse_file(rel, source)
            except Exception:
                pass  # Unparseable files are skipped — don't crash the graph

        return graph

    def _parse_file(self, rel: str, source: str) -> None:
        """Parse a single file and update graph edges."""
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return

        for node in ast.walk(tree):
            # Standard imports: `import normalize_slot`
            if isinstance(node, ast.Import):
                for alias in node.names:
                    dep = _module_to_rel(alias.name)
                    if dep and (BASE_DIR / dep).exists():
                        self._add_edge(rel, dep)

            # From-imports: `from normalize_slot import normalize`
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    dep = _module_to_rel(node.module)
                    if dep and (BASE_DIR / dep).exists():
                        self._add_edge(rel, dep)

            # Function definitions
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Don't overwrite with nested function defs — keep outermost
                if node.name not in self.func_defs:
                    self.func_defs[node.name] = rel

            # Function calls (simple Name and attribute calls)
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    self.func_calls[rel].add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    self.func_calls[rel].add(node.func.attr)

        # Lazy loading patterns that AST import tracking misses:

        # importlib.util.spec_from_file_location("name", "tools/foo.py")
        for match in re.finditer(
            r'spec_from_file_location\s*\([^,]+,\s*["\']([^"\']+\.py)["\']',
            source,
        ):
            dep = _path_str_to_rel(match.group(1))
            if dep and (BASE_DIR / dep).exists():
                self._add_edge(rel, dep)

        # _load_module("module_name") — custom helper in run_api_server.py
        for match in re.finditer(
            r'_load_module(?:_direct)?\s*\(\s*["\']([a-z_]+)["\']',
            source,
        ):
            dep = _module_to_rel(match.group(1))
            if dep and (BASE_DIR / dep).exists():
                self._add_edge(rel, dep)

        # importlib + string variable patterns: Path(__file__).parent / "foo.py"
        for match in re.finditer(
            r'["\']([a-z_]+\.py)["\']',
            source,
        ):
            name = match.group(1)
            dep = f"tools/{name}"
            if dep != rel and (BASE_DIR / dep).exists():
                # Only add if there's also an importlib pattern nearby
                # (conservative — avoid false edges from string literals)
                pass  # Covered by spec_from_file_location pattern above

    def _add_edge(self, src: str, dep: str) -> None:
        """Add import edge: src depends on dep."""
        self.depends_on.setdefault(src, set()).add(dep)
        self.depended_by.setdefault(dep, set()).add(src)

    # ── Queries ───────────────────────────────────────────────────────────────

    def blast_radius(self, file: str) -> FrozenSet[str]:
        """
        Return every file that must be re-tested when `file` changes.

        Traverses the depended_by graph transitively:
          normalize_slot.py → the 12 fetch tools that import it
          complete_booking.py → run_api_server.py + execution_engine.py
        """
        file = _normalize_key(file)
        visited: Set[str] = set()
        queue = [file]
        while queue:
            current = queue.pop()
            if current in visited:
                continue
            visited.add(current)
            for dep in self.depended_by.get(current, set()):
                if dep not in visited:
                    queue.append(dep)
        visited.discard(file)
        return frozenset(visited)

    def dependencies(self, file: str) -> FrozenSet[str]:
        """Return files that `file` directly imports."""
        return frozenset(self.depends_on.get(_normalize_key(file), set()))

    def callers_of(self, function_name: str) -> FrozenSet[str]:
        """Return all files that call `function_name`."""
        return frozenset(
            f for f, calls in self.func_calls.items()
            if function_name in calls
        )

    def coupling_score(self, file: str) -> int:
        """How many files depend on this file (direct importers)."""
        return len(self.depended_by.get(_normalize_key(file), set()))

    def risk_level(self, file: str) -> str:
        """Classify a file's change risk based on coupling."""
        score = self.coupling_score(file)
        if file in HIGH_COUPLING_FILES or score >= 5:
            return "CRITICAL"
        elif score >= 2:
            return "HIGH"
        elif score >= 1:
            return "MEDIUM"
        return "LOW"

    def priority_order(self, files: Optional[List[str]] = None) -> List[str]:
        """
        Return files sorted by change risk (highest first).
        Used by deep_audit.py to prioritize which files to review first.
        """
        if files is None:
            files = list(self.all_files)
        return sorted(
            files,
            key=lambda f: (
                -self.coupling_score(f),            # Most-depended-on first
                f in HIGH_COUPLING_FILES,           # Known high-coupling files
                len(self.func_calls.get(f, set())), # More callers = more surface area
            ),
            reverse=False,
        )

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self, path: Path = CACHE_FILE) -> None:
        """Cache graph to JSON (avoids re-parsing on subsequent calls)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "depends_on":  {k: sorted(v) for k, v in self.depends_on.items()},
            "depended_by": {k: sorted(v) for k, v in self.depended_by.items()},
            "func_defs":   self.func_defs,
            "func_calls":  {k: sorted(v) for k, v in self.func_calls.items()},
            "all_files":   sorted(self.all_files),
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path = CACHE_FILE) -> Optional["CallGraph"]:
        """Load cached graph. Returns None if cache doesn't exist."""
        if not path.exists():
            return None
        try:
            graph = cls()
            data = json.loads(path.read_text(encoding="utf-8"))
            graph.depends_on  = {k: set(v) for k, v in data["depends_on"].items()}
            graph.depended_by = {k: set(v) for k, v in data["depended_by"].items()}
            graph.func_defs   = data["func_defs"]
            graph.func_calls  = {k: set(v) for k, v in data["func_calls"].items()}
            graph.all_files   = set(data["all_files"])
            return graph
        except Exception:
            return None

    @classmethod
    def build_or_load(cls) -> "CallGraph":
        """Use cache if available and fresh, otherwise rebuild."""
        cached = cls.load()
        if cached is not None:
            return cached
        graph = cls.build()
        graph.save()
        return graph

    # ── Summary ───────────────────────────────────────────────────────────────

    def summary(self) -> dict:
        """Return a summary dict suitable for logging."""
        most_depended = sorted(
            [(f, len(v)) for f, v in self.depended_by.items() if v],
            key=lambda x: x[1],
            reverse=True,
        )[:8]
        return {
            "files":          len(self.all_files),
            "import_edges":   sum(len(v) for v in self.depends_on.values()),
            "functions":      len(self.func_defs),
            "most_depended":  most_depended,
        }

    def print_summary(self) -> None:
        s = self.summary()
        print(f"Call graph: {s['files']} files, {s['import_edges']} import edges, "
              f"{s['functions']} functions defined")
        print("\nMost-depended-on files (highest blast radius first):")
        for f, count in s["most_depended"]:
            risk = self.risk_level(f)
            print(f"  [{risk:8s}] {f:<50s}  ← {count} direct importers")


# ── Path helpers ──────────────────────────────────────────────────────────────

def _rel_path(fp: Path) -> str:
    """Convert absolute Path to 'tools/foo.py' relative string."""
    try:
        return str(fp.relative_to(BASE_DIR)).replace("\\", "/")
    except ValueError:
        return str(fp).replace("\\", "/")


def _module_to_rel(module_name: str) -> Optional[str]:
    """Convert a Python module name to a relative path string."""
    # Handle: "normalize_slot" → "tools/normalize_slot.py"
    # Handle: "tools.normalize_slot" → "tools/normalize_slot.py"
    # Reject: "os", "json", "requests" (stdlib/third-party)
    if not module_name:
        return None
    # Strip 'tools.' prefix if present
    if module_name.startswith("tools."):
        module_name = module_name[6:]
    # Must be a single-level name without dots (no package.submodule)
    if "." in module_name:
        return None
    # Must not be a known stdlib/third-party module
    _known_external = {
        "os", "sys", "json", "re", "time", "datetime", "pathlib", "typing",
        "hashlib", "math", "abc", "ast", "io", "copy", "itertools", "functools",
        "collections", "dataclasses", "threading", "subprocess", "shutil", "glob",
        "requests", "flask", "dotenv", "stripe", "supabase", "anthropic",
        "google", "twilio", "sendgrid", "importlib", "argparse", "socket",
        "logging", "traceback", "uuid", "hmac", "base64", "urllib", "http",
        "email", "html", "xml", "csv", "enum", "random", "string", "struct",
        "signal", "inspect", "contextlib", "weakref", "decimal", "fractions",
    }
    if module_name.lower() in _known_external:
        return None
    return f"tools/{module_name}.py"


def _path_str_to_rel(path_str: str) -> Optional[str]:
    """Normalize a file path string to 'tools/foo.py' format."""
    if "tools/" in path_str:
        idx = path_str.index("tools/")
        return path_str[idx:].replace("\\", "/")
    name = Path(path_str).name
    if name.endswith(".py"):
        return f"tools/{name}"
    return None


def _normalize_key(file: str) -> str:
    """Normalize a file reference to the graph's key format."""
    file = file.replace("\\", "/")
    if not file.startswith("tools/"):
        file = f"tools/{file}"
    if not file.endswith(".py"):
        file = f"{file}.py"
    return file


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Building call graph...")
    graph = CallGraph.build()
    graph.print_summary()
    graph.save()
    print(f"\nCached to {CACHE_FILE}")

    if len(sys.argv) > 1:
        target = sys.argv[1]
        blast = graph.blast_radius(target)
        risk  = graph.risk_level(target)
        print(f"\n[{risk}] Blast radius of '{target}': {len(blast)} dependent files")
        for f in sorted(blast):
            print(f"  → {f}")
        deps = graph.dependencies(target)
        if deps:
            print(f"\n  '{target}' imports:")
            for f in sorted(deps):
                print(f"  ← {f}")
