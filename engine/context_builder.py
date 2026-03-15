"""
ReviewCrew — Project Context Builder

Scans the checked-out repository BEFORE running agents to build a rich
architectural picture. Runs once per review, no API calls, pure file I/O.

Produces:
  tech_stack        — language, framework, runtime, build tool, test framework
  dependencies      — external packages with versions; flags security-relevant ones
  directory_map     — purpose of every significant directory (controller/service/repo/...)
  entry_points      — main classes, controllers, route files
  api_contracts     — REST endpoints, OpenAPI files, proto files, GraphQL schemas
  db_schema         — ORM models, migration files, table names, relationships
  service_topology  — databases, external APIs, message queues, caches (from config scans)
  module_graph      — for each changed file: what it imports (upstream) + what imports it (downstream)
  test_coverage_map — which test files cover each changed source file
  blast_radius      — how many files are downstream of the changed files (impact score)
"""

from __future__ import annotations

import re
import json
import subprocess
from pathlib import Path
from collections import defaultdict

# ─── Constants ────────────────────────────────────────────────────────────────

SKIP_DIRS = {
    "node_modules", ".git", "target", "build", "dist", "__pycache__",
    ".gradle", ".mvn", "vendor", "venv", ".venv", "env", ".env",
    "coverage", ".nyc_output", "out", "bin", "obj", ".idea", ".vscode",
}

MAX_FILE_SIZE = 200_000   # skip files > 200KB
MAX_SCAN_FILES = 2_000    # stop after scanning this many files for imports


# ─── Tech Stack Detection ─────────────────────────────────────────────────────

def detect_tech_stack(repo_root: Path) -> dict:
    """
    Identify language, framework, build tool, runtime, and test framework
    by inspecting manifest and config files. No API calls.
    """
    stack = {
        "language": "unknown",
        "languages": [],
        "framework": "unknown",
        "build_tool": "unknown",
        "runtime": "unknown",
        "test_framework": "unknown",
        "version": None,
        "manifest_file": None,
    }

    # ── Java / Kotlin / Scala ──
    pom = repo_root / "pom.xml"
    if pom.exists():
        stack.update({"language": "java", "build_tool": "maven", "runtime": "jvm"})
        stack["manifest_file"] = "pom.xml"
        content = _safe_read(pom)
        stack["framework"] = _detect_java_framework(content)
        stack["test_framework"] = _detect_java_test_framework(content)
        m = re.search(r"<spring-boot\.version>(.*?)</spring-boot\.version>|"
                      r"<version>(.*?)</version>.*?spring-boot", content)
        if m:
            stack["version"] = (m.group(1) or m.group(2) or "").strip()
        if re.search(r"\.kt\b", content) or (repo_root / "src").exists() and \
           any(f.suffix == ".kt" for f in _iter_files(repo_root / "src", max_files=100)):
            stack["language"] = "kotlin"
        return _add_secondary_languages(stack, repo_root)

    gradle = repo_root / "build.gradle"
    gradle_kts = repo_root / "build.gradle.kts"
    if gradle.exists() or gradle_kts.exists():
        content = _safe_read(gradle if gradle.exists() else gradle_kts)
        stack.update({"build_tool": "gradle", "runtime": "jvm"})
        stack["manifest_file"] = str(gradle if gradle.exists() else gradle_kts)
        stack["language"] = "kotlin" if gradle_kts.exists() else "java"
        stack["framework"] = _detect_java_framework(content)
        stack["test_framework"] = _detect_java_test_framework(content)
        return _add_secondary_languages(stack, repo_root)

    # ── Node.js ──
    pkg = repo_root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            data = {}
        all_deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        stack.update({
            "language": "javascript",
            "build_tool": "npm" if (repo_root / "package-lock.json").exists() else
                          "yarn" if (repo_root / "yarn.lock").exists() else
                          "pnpm" if (repo_root / "pnpm-lock.yaml").exists() else "npm",
            "runtime": "nodejs",
            "manifest_file": "package.json",
        })
        if any(d in all_deps for d in ("typescript", "@types/node")):
            stack["language"] = "typescript"
        stack["framework"] = _detect_node_framework(all_deps)
        stack["test_framework"] = _detect_node_test_framework(all_deps)
        stack["version"] = data.get("engines", {}).get("node", None)
        return _add_secondary_languages(stack, repo_root)

    # ── Python ──
    pyproject = repo_root / "pyproject.toml"
    requirements = repo_root / "requirements.txt"
    setup_py = repo_root / "setup.py"
    if pyproject.exists() or requirements.exists() or setup_py.exists():
        stack.update({"language": "python", "runtime": "cpython"})
        manifest = pyproject if pyproject.exists() else (
            requirements if requirements.exists() else setup_py)
        stack["manifest_file"] = str(manifest.name)
        content = _safe_read(manifest)
        stack["build_tool"] = "poetry" if "poetry" in content else \
                               "pip" if requirements.exists() else "setuptools"
        stack["framework"] = _detect_python_framework(content)
        stack["test_framework"] = "pytest" if "pytest" in content else \
                                   "unittest" if "unittest" in content else "pytest"
        return _add_secondary_languages(stack, repo_root)

    # ── Go ──
    gomod = repo_root / "go.mod"
    if gomod.exists():
        content = _safe_read(gomod)
        stack.update({"language": "go", "build_tool": "go modules",
                      "runtime": "go", "manifest_file": "go.mod"})
        m = re.search(r"^go\s+(\S+)", content, re.MULTILINE)
        if m:
            stack["version"] = m.group(1)
        stack["framework"] = _detect_go_framework(content)
        stack["test_framework"] = "testing (stdlib)"
        return _add_secondary_languages(stack, repo_root)

    # ── Rust ──
    cargo = repo_root / "Cargo.toml"
    if cargo.exists():
        content = _safe_read(cargo)
        stack.update({"language": "rust", "build_tool": "cargo",
                      "runtime": "native", "manifest_file": "Cargo.toml"})
        stack["framework"] = _detect_rust_framework(content)
        stack["test_framework"] = "cargo test (built-in)"
        return stack

    # ── Ruby ──
    gemfile = repo_root / "Gemfile"
    if gemfile.exists():
        content = _safe_read(gemfile)
        stack.update({"language": "ruby", "build_tool": "bundler",
                      "runtime": "mri", "manifest_file": "Gemfile"})
        stack["framework"] = "rails" if "rails" in content.lower() else \
                              "sinatra" if "sinatra" in content.lower() else "unknown"
        stack["test_framework"] = "rspec" if "rspec" in content.lower() else "minitest"
        return stack

    # ── PHP ──
    composer = repo_root / "composer.json"
    if composer.exists():
        try:
            data = json.loads(composer.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            data = {}
        all_deps = {**data.get("require", {}), **data.get("require-dev", {})}
        stack.update({"language": "php", "build_tool": "composer",
                      "runtime": "php", "manifest_file": "composer.json"})
        stack["framework"] = "laravel" if "laravel/framework" in all_deps else \
                              "symfony" if "symfony/framework-bundle" in all_deps else "unknown"
        return stack

    # ── C# / .NET ──
    csproj_files = list(repo_root.glob("**/*.csproj"))
    if csproj_files:
        content = _safe_read(csproj_files[0])
        stack.update({"language": "csharp", "build_tool": "dotnet",
                      "runtime": "dotnet", "manifest_file": str(csproj_files[0].name)})
        stack["framework"] = "aspnet-core" if "Microsoft.AspNetCore" in content else "dotnet"
        return stack

    return stack


def _detect_java_framework(content: str) -> str:
    cl = content.lower()
    if "spring-boot" in cl or "org.springframework.boot" in cl:
        return "spring-boot"
    if "quarkus" in cl:
        return "quarkus"
    if "micronaut" in cl:
        return "micronaut"
    if "jakarta" in cl or "javax.ws.rs" in cl:
        return "jakarta-ee"
    return "java-se"


def _detect_java_test_framework(content: str) -> str:
    cl = content.lower()
    if "junit-jupiter" in cl or "junit5" in cl or "5." in cl and "junit" in cl:
        return "junit5"
    if "junit" in cl:
        return "junit4"
    if "testng" in cl:
        return "testng"
    return "junit5"


def _detect_node_framework(deps: dict) -> str:
    if "next" in deps:
        return "next.js"
    if "@nestjs/core" in deps or "@nestjs/common" in deps:
        return "nest.js"
    if "nuxt" in deps:
        return "nuxt.js"
    if "express" in deps:
        return "express"
    if "fastify" in deps:
        return "fastify"
    if "koa" in deps:
        return "koa"
    if "react" in deps:
        return "react (spa)"
    if "vue" in deps:
        return "vue"
    if "@angular/core" in deps:
        return "angular"
    return "node.js"


def _detect_node_test_framework(deps: dict) -> str:
    if "jest" in deps:
        return "jest"
    if "vitest" in deps:
        return "vitest"
    if "mocha" in deps:
        return "mocha"
    if "@playwright/test" in deps:
        return "playwright"
    return "jest"


def _detect_python_framework(content: str) -> str:
    cl = content.lower()
    if "django" in cl:
        return "django"
    if "fastapi" in cl:
        return "fastapi"
    if "flask" in cl:
        return "flask"
    if "tornado" in cl:
        return "tornado"
    if "starlette" in cl:
        return "starlette"
    if "aiohttp" in cl:
        return "aiohttp"
    return "python"


def _detect_go_framework(content: str) -> str:
    if "gin-gonic/gin" in content:
        return "gin"
    if "labstack/echo" in content:
        return "echo"
    if "gofiber/fiber" in content:
        return "fiber"
    if "gorilla/mux" in content:
        return "gorilla-mux"
    if "go-chi/chi" in content:
        return "chi"
    return "net/http"


def _detect_rust_framework(content: str) -> str:
    if "actix-web" in content:
        return "actix-web"
    if "axum" in content:
        return "axum"
    if "rocket" in content:
        return "rocket"
    if "warp" in content:
        return "warp"
    return "rust"


def _add_secondary_languages(stack: dict, repo_root: Path) -> dict:
    """Detect if the repo mixes multiple languages."""
    secondary = []
    ext_counts: dict[str, int] = defaultdict(int)
    for f in _iter_files(repo_root, max_files=500):
        ext_counts[f.suffix.lower()] += 1

    lang_map = {
        ".py": "python", ".ts": "typescript", ".js": "javascript",
        ".java": "java", ".kt": "kotlin", ".go": "go", ".rs": "rust",
        ".rb": "ruby", ".php": "php", ".cs": "csharp", ".cpp": "c++",
        ".sh": "shell", ".sql": "sql", ".tf": "terraform",
    }
    for ext, lang in lang_map.items():
        if ext_counts.get(ext, 0) >= 3 and lang != stack["language"]:
            secondary.append(lang)

    stack["languages"] = [stack["language"]] + secondary[:4]
    return stack


# ─── Dependency Analysis ──────────────────────────────────────────────────────

# Libraries with known historical CVEs — flag if version is old / unpinned
SECURITY_SENSITIVE_LIBS = {
    # Java
    "log4j", "log4j-core", "log4j2", "jackson-databind", "spring-core",
    "spring-security", "commons-collections", "xstream",
    # Node
    "lodash", "moment", "axios", "express", "jsonwebtoken", "passport",
    "multer", "helmet", "node-fetch", "request",
    # Python
    "django", "flask", "requests", "cryptography", "paramiko", "pyyaml",
    "pillow", "sqlalchemy", "jinja2", "werkzeug",
    # Go
    "golang.org/x/crypto", "golang.org/x/net",
    # Ruby
    "rack", "rails", "nokogiri", "activesupport",
}


def parse_dependencies(repo_root: Path, tech_stack: dict) -> dict:
    """
    Parse dependency manifests to extract external packages.
    Flags packages in SECURITY_SENSITIVE_LIBS for extra agent scrutiny.
    """
    deps = {"external": [], "security_relevant": [], "dev_only": [], "total_count": 0}

    lang = tech_stack.get("language", "")

    if lang in ("java", "kotlin"):
        _parse_maven_or_gradle(repo_root, deps)
    elif lang in ("javascript", "typescript"):
        _parse_npm(repo_root, deps)
    elif lang == "python":
        _parse_python_deps(repo_root, deps)
    elif lang == "go":
        _parse_go_deps(repo_root, deps)
    elif lang == "ruby":
        _parse_gemfile(repo_root, deps)

    deps["total_count"] = len(deps["external"])

    # Flag security-sensitive libs
    for dep in deps["external"]:
        name_lower = dep["name"].lower().replace("-", "").replace("_", "").replace(".", "")
        for lib in SECURITY_SENSITIVE_LIBS:
            lib_norm = lib.lower().replace("-", "").replace("_", "").replace(".", "")
            if lib_norm in name_lower or name_lower in lib_norm:
                deps["security_relevant"].append(f"{dep['name']}:{dep.get('version', 'unpinned')}")
                break

    return deps


def _parse_maven_or_gradle(repo_root: Path, deps: dict) -> None:
    pom = repo_root / "pom.xml"
    if pom.exists():
        content = _safe_read(pom)
        # Extract <groupId>:<artifactId>:<version>
        artifacts = re.findall(
            r"<groupId>([^<]+)</groupId>\s*<artifactId>([^<]+)</artifactId>"
            r"(?:\s*<version>([^<]+)</version>)?",
            content,
        )
        for group, artifact, version in artifacts[:100]:
            deps["external"].append({
                "name": f"{group.strip()}:{artifact.strip()}",
                "version": version.strip() if version else "inherited",
            })

    gradle = repo_root / "build.gradle"
    gradle_kts = repo_root / "build.gradle.kts"
    for gf in [gradle, gradle_kts]:
        if gf.exists():
            content = _safe_read(gf)
            matches = re.findall(
                r"""(?:implementation|testImplementation|runtimeOnly|compileOnly)"""
                r"""[( ]["']([^'":\n]+):([^'":\n]+)(?::([^'")\n]+))?['" )]""",
                content,
            )
            for group, artifact, version in matches[:100]:
                deps["external"].append({
                    "name": f"{group.strip()}:{artifact.strip()}",
                    "version": version.strip() if version else "variable",
                })


def _parse_npm(repo_root: Path, deps: dict) -> None:
    pkg = repo_root / "package.json"
    if not pkg.exists():
        return
    try:
        data = json.loads(pkg.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return
    for name, version in data.get("dependencies", {}).items():
        deps["external"].append({"name": name, "version": version})
    for name, version in data.get("devDependencies", {}).items():
        deps["external"].append({"name": name, "version": version})
        deps["dev_only"].append(name)


def _parse_python_deps(repo_root: Path, deps: dict) -> None:
    req = repo_root / "requirements.txt"
    if req.exists():
        for line in _safe_read(req).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^([A-Za-z0-9_.-]+)([>=<!~^].+)?", line)
            if m:
                deps["external"].append({
                    "name": m.group(1),
                    "version": (m.group(2) or "").strip() or "unpinned",
                })

    pyproject = repo_root / "pyproject.toml"
    if pyproject.exists():
        content = _safe_read(pyproject)
        for m in re.finditer(r'"([A-Za-z0-9_.-]+)\s*([>=<!~^][^"]*)"', content):
            deps["external"].append({"name": m.group(1), "version": m.group(2).strip()})


def _parse_go_deps(repo_root: Path, deps: dict) -> None:
    gomod = repo_root / "go.mod"
    if not gomod.exists():
        return
    for line in _safe_read(gomod).splitlines():
        m = re.match(r"\s+([^\s]+)\s+v([^\s]+)", line)
        if m:
            deps["external"].append({"name": m.group(1), "version": "v" + m.group(2)})


def _parse_gemfile(repo_root: Path, deps: dict) -> None:
    gemfile = repo_root / "Gemfile"
    if not gemfile.exists():
        return
    for line in _safe_read(gemfile).splitlines():
        m = re.match(r"""gem\s+['"]([^'"]+)['"](?:,\s*['"]([^'"]+)['"])?""", line.strip())
        if m:
            deps["external"].append({
                "name": m.group(1),
                "version": m.group(2) or "unpinned",
            })


# ─── Directory Map ────────────────────────────────────────────────────────────

DIR_PURPOSE_HINTS = {
    # Controllers / entry points
    "controller": "REST controllers / API entry points",
    "controllers": "REST controllers / API entry points",
    "handler": "request handlers",
    "handlers": "request handlers",
    "route": "route definitions",
    "routes": "route definitions",
    "view": "view layer / templates",
    "views": "view layer / templates",
    "resolver": "GraphQL resolvers",

    # Business logic
    "service": "business logic / service layer",
    "services": "business logic / service layer",
    "usecase": "use case / application layer",
    "usecases": "use case / application layer",
    "domain": "domain model / business entities",
    "business": "business logic",
    "application": "application layer",

    # Data access
    "repository": "data access / repository layer",
    "repositories": "data access / repository layer",
    "repo": "data access / repository layer",
    "dao": "data access objects",
    "store": "data store layer",
    "persistence": "persistence / data access",

    # Models / schema
    "model": "data models / entities",
    "models": "data models / entities",
    "entity": "JPA / ORM entities",
    "entities": "JPA / ORM entities",
    "schema": "schema definitions",
    "dto": "data transfer objects",
    "dtos": "data transfer objects",

    # Infrastructure
    "config": "application configuration",
    "configuration": "application configuration",
    "middleware": "middleware / interceptors",
    "interceptor": "request interceptors",
    "filter": "request filters",
    "gateway": "external API gateway layer",
    "client": "external service clients",
    "clients": "external service clients",
    "adapter": "adapters / integration layer",
    "adapters": "adapters / integration layer",
    "infrastructure": "infrastructure / integration layer",

    # Utilities
    "util": "utility functions",
    "utils": "utility functions",
    "helper": "helper functions",
    "helpers": "helper functions",
    "common": "shared/common utilities",
    "shared": "shared code",

    # Tests
    "test": "unit / integration tests",
    "tests": "unit / integration tests",
    "spec": "test specifications",
    "__tests__": "Jest test files",
    "e2e": "end-to-end tests",

    # API definitions
    "proto": "gRPC proto definitions",
    "protos": "gRPC proto definitions",
    "graphql": "GraphQL schema / resolvers",
    "openapi": "OpenAPI / Swagger definitions",
    "swagger": "Swagger / OpenAPI definitions",

    # DB
    "migration": "database migrations",
    "migrations": "database migrations",
    "seed": "database seed data",
    "seeds": "database seed data",
    "flyway": "Flyway database migrations",
    "liquibase": "Liquibase database changesets",

    # Events / messaging
    "event": "domain events / event handlers",
    "events": "domain events / event handlers",
    "message": "message handlers",
    "queue": "queue workers / consumers",
    "consumer": "message queue consumers",
    "producer": "message queue producers",
    "subscriber": "event subscribers",
    "publisher": "event publishers",

    # Jobs / background tasks
    "job": "background jobs / scheduled tasks",
    "jobs": "background jobs / scheduled tasks",
    "task": "async tasks",
    "tasks": "async tasks",
    "worker": "background workers",
    "workers": "background workers",
    "cron": "scheduled cron jobs",
    "scheduler": "task scheduler",
}


def build_directory_map(repo_root: Path) -> dict[str, str]:
    """
    Walk the directory tree and assign a purpose label to each significant directory.
    Caps at 2 levels deep to avoid noise.
    """
    directory_map: dict[str, str] = {}

    for item in _iter_dirs(repo_root, max_depth=4):
        rel = item.relative_to(repo_root)
        parts = rel.parts
        if not parts:
            continue

        # Check each component of the path for a purpose hint
        for part in reversed(parts):  # leaf name is most specific
            lower = part.lower()
            if lower in DIR_PURPOSE_HINTS:
                directory_map[str(rel)] = DIR_PURPOSE_HINTS[lower]
                break
            # Partial match
            for key, purpose in DIR_PURPOSE_HINTS.items():
                if key in lower and len(key) >= 4:
                    directory_map[str(rel)] = purpose
                    break

    return directory_map


# ─── Entry Points ─────────────────────────────────────────────────────────────

def find_entry_points(repo_root: Path, tech_stack: dict) -> list[dict]:
    """
    Find main classes, controllers, route files, and app bootstraps.
    """
    lang = tech_stack.get("language", "")
    framework = tech_stack.get("framework", "")
    entry_points = []

    if lang in ("java", "kotlin"):
        entry_points.extend(_find_java_entry_points(repo_root, framework))
    elif lang in ("javascript", "typescript"):
        entry_points.extend(_find_node_entry_points(repo_root, framework))
    elif lang == "python":
        entry_points.extend(_find_python_entry_points(repo_root, framework))
    elif lang == "go":
        entry_points.extend(_find_go_entry_points(repo_root))

    return entry_points[:30]


def _find_java_entry_points(repo_root: Path, framework: str) -> list[dict]:
    entries = []
    for f in _iter_files(repo_root, exts={".java", ".kt"}, max_files=500):
        content = _safe_read(f, max_bytes=4000)
        rel = str(f.relative_to(repo_root))
        if "@SpringBootApplication" in content or "public static void main" in content:
            entries.append({"file": rel, "type": "main", "detail": "Application bootstrap"})
        elif "@RestController" in content or "@Controller" in content:
            # Extract @RequestMapping paths
            paths = re.findall(r'@(?:Request|Get|Post|Put|Delete|Patch)Mapping\s*\(\s*["\']([^"\']+)', content)
            entries.append({"file": rel, "type": "controller", "routes": paths[:10]})
        elif "@Component" in content and "Listener" in f.name:
            entries.append({"file": rel, "type": "event_listener"})
        elif "@KafkaListener" in content or "@RabbitListener" in content or "@SqsListener" in content:
            entries.append({"file": rel, "type": "message_consumer"})
        elif "@Scheduled" in content:
            entries.append({"file": rel, "type": "scheduled_job"})
    return entries


def _find_node_entry_points(repo_root: Path, framework: str) -> list[dict]:
    entries = []
    for f in _iter_files(repo_root, exts={".ts", ".js"}, max_files=500):
        if any(skip in str(f) for skip in ["test", "spec", "__tests__", ".d.ts", "node_modules"]):
            continue
        content = _safe_read(f, max_bytes=5000)
        rel = str(f.relative_to(repo_root))

        if "app.listen(" in content or "server.listen(" in content or \
           "NestFactory.create(" in content or "createServer(" in content:
            entries.append({"file": rel, "type": "main"})
        elif re.search(r"router\.(get|post|put|delete|patch)\(", content) or \
             re.search(r"@(Get|Post|Put|Delete|Patch)\(", content):
            routes = re.findall(r"""(?:router|app)\.\w+\s*\(\s*['"]([^'"]+)""", content)
            routes += re.findall(r"""@(?:Get|Post|Put|Delete|Patch)\s*\(\s*['"]([^'"]+)""", content)
            entries.append({"file": rel, "type": "controller", "routes": routes[:10]})
    return entries


def _find_python_entry_points(repo_root: Path, framework: str) -> list[dict]:
    entries = []
    for f in _iter_files(repo_root, exts={".py"}, max_files=400):
        if "test" in str(f).lower():
            continue
        content = _safe_read(f, max_bytes=5000)
        rel = str(f.relative_to(repo_root))

        if 'if __name__ == "__main__"' in content or "if __name__ == '__main__'" in content:
            entries.append({"file": rel, "type": "main"})
        elif re.search(r"@(?:app|router|blueprint)\.(get|post|put|delete|patch)\(", content):
            routes = re.findall(r"""@\w+\.(?:get|post|put|delete|patch)\s*\(['"]([^'"]+)""", content)
            entries.append({"file": rel, "type": "controller", "routes": routes[:10]})
        elif "@app.route(" in content or "@router.route(" in content:
            routes = re.findall(r"""@\w+\.route\s*\(['"]([^'"]+)""", content)
            entries.append({"file": rel, "type": "controller", "routes": routes[:10]})
    return entries


def _find_go_entry_points(repo_root: Path) -> list[dict]:
    entries = []
    for f in _iter_files(repo_root, exts={".go"}, max_files=300):
        content = _safe_read(f, max_bytes=3000)
        rel = str(f.relative_to(repo_root))
        if "func main()" in content:
            entries.append({"file": rel, "type": "main"})
        elif re.search(r'\.(GET|POST|PUT|DELETE|PATCH)\s*\("', content):
            routes = re.findall(r"""\.(?:GET|POST|PUT|DELETE|PATCH)\s*\(\s*"([^"]+)""", content)
            entries.append({"file": rel, "type": "controller", "routes": routes[:10]})
    return entries


# ─── API Contracts ────────────────────────────────────────────────────────────

def find_api_contracts(repo_root: Path, tech_stack: dict) -> dict:
    """
    Locate OpenAPI/Swagger specs, gRPC proto files, GraphQL schemas,
    and extract REST endpoint definitions.
    """
    contracts = {
        "openapi_files": [],
        "proto_files": [],
        "graphql_files": [],
        "endpoint_count": 0,
        "endpoints_sample": [],
    }

    # OpenAPI / Swagger
    for pattern in ["**/*.yaml", "**/*.yml", "**/*.json"]:
        for f in repo_root.glob(pattern):
            if any(skip in str(f) for skip in SKIP_DIRS):
                continue
            content_start = _safe_read(f, max_bytes=500)
            if "openapi:" in content_start or "swagger:" in content_start:
                contracts["openapi_files"].append(str(f.relative_to(repo_root)))

    # gRPC proto files
    for f in repo_root.rglob("*.proto"):
        if not any(skip in str(f) for skip in SKIP_DIRS):
            contracts["proto_files"].append(str(f.relative_to(repo_root)))

    # GraphQL
    for pattern in ["**/*.graphql", "**/*.gql"]:
        for f in repo_root.glob(pattern):
            if not any(skip in str(f) for skip in SKIP_DIRS):
                contracts["graphql_files"].append(str(f.relative_to(repo_root)))
    for f in _iter_files(repo_root, exts={".ts", ".js", ".py"}, max_files=200):
        content = _safe_read(f, max_bytes=2000)
        if "buildSchema(" in content or "gql`" in content or "graphene" in content or \
           "strawberry.type" in content:
            contracts["graphql_files"].append(str(f.relative_to(repo_root)))

    # Count REST endpoints from entry points
    lang = tech_stack.get("language", "")
    ep_patterns = {
        "java": r'@(?:Request|Get|Post|Put|Delete|Patch)Mapping\s*\(\s*["\']([^"\']+)',
        "kotlin": r'@(?:Request|Get|Post|Put|Delete|Patch)Mapping\s*\(\s*["\']([^"\']+)',
        "python": r'@\w+\.(?:get|post|put|delete|patch|route)\s*\(["\']([^"\']+)',
        "typescript": r"""(?:router|app)\.\w+\s*\(\s*['"]([^'"]+)|@(?:Get|Post|Put|Delete|Patch)\s*\(\s*['"]([^'"]+)""",
        "javascript": r"""(?:router|app)\.\w+\s*\(\s*['"]([^'"]+)""",
        "go": r"""\.(?:GET|POST|PUT|DELETE|PATCH)\s*\(\s*"([^"]+)""",
    }

    if lang in ep_patterns:
        all_endpoints = []
        for f in _iter_files(repo_root, max_files=300):
            if any(skip in str(f) for skip in ["test", "spec", "node_modules"]):
                continue
            content = _safe_read(f, max_bytes=10000)
            for m in re.finditer(ep_patterns[lang], content):
                endpoint = m.group(1) or (m.lastindex > 1 and m.group(2))
                if endpoint:
                    all_endpoints.append(endpoint)
        contracts["endpoint_count"] = len(all_endpoints)
        contracts["endpoints_sample"] = all_endpoints[:20]

    return contracts


# ─── DB Schema ────────────────────────────────────────────────────────────────

def find_db_schema(repo_root: Path, tech_stack: dict) -> dict:
    """
    Find ORM models, migration files, table names, and schema relationships.
    """
    schema = {
        "orm": "unknown",
        "entities": [],
        "migration_files": [],
        "tables_detected": [],
    }

    lang = tech_stack.get("language", "")
    framework = tech_stack.get("framework", "")

    # Migration files
    for pattern in ["**/migrations/*.py", "**/migrations/*.sql", "**/migration/*.sql",
                     "**/db/migrate/*.rb", "**/flyway/*.sql", "**/liquibase/**/*.xml",
                     "**/changelog/*.sql", "**/V*__*.sql"]:
        for f in repo_root.glob(pattern):
            if not any(skip in str(f) for skip in SKIP_DIRS):
                schema["migration_files"].append(str(f.relative_to(repo_root)))
                # Extract table names from CREATE TABLE
                content = _safe_read(f, max_bytes=5000)
                for m in re.finditer(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"']?(\w+)[`\"']?", content, re.IGNORECASE):
                    if m.group(1).lower() not in ("schema", "database", "index"):
                        schema["tables_detected"].append(m.group(1))

    # Java JPA / Hibernate entities
    if lang in ("java", "kotlin"):
        schema["orm"] = "jpa/hibernate"
        for f in _iter_files(repo_root, exts={".java", ".kt"}, max_files=300):
            content = _safe_read(f, max_bytes=3000)
            if "@Entity" in content or "@Table" in content:
                m = re.search(r'@Table\s*\(\s*name\s*=\s*["\'](\w+)', content)
                table_name = m.group(1) if m else f.stem.lower() + "s"
                schema["entities"].append({
                    "class": f.stem,
                    "file": str(f.relative_to(repo_root)),
                    "table": table_name,
                })
                if table_name not in schema["tables_detected"]:
                    schema["tables_detected"].append(table_name)

    # Python SQLAlchemy / Django ORM
    elif lang == "python":
        for f in _iter_files(repo_root, exts={".py"}, max_files=300):
            content = _safe_read(f, max_bytes=5000)
            if "models.Model" in content or "declarative_base" in content or \
               "db.Model" in content or "SQLModel" in content:
                schema["orm"] = "django-orm" if "models.Model" in content else "sqlalchemy"
                # Extract class names (models)
                for m in re.finditer(r"class\s+(\w+)\s*\(.*?[Mm]odel", content):
                    schema["entities"].append({
                        "class": m.group(1),
                        "file": str(f.relative_to(repo_root)),
                    })

    # TypeScript/Prisma
    elif lang in ("typescript", "javascript"):
        for f in _iter_files(repo_root, exts={".prisma"}, max_files=10):
            schema["orm"] = "prisma"
            content = _safe_read(f)
            for m in re.finditer(r"model\s+(\w+)\s*\{", content):
                schema["entities"].append({
                    "class": m.group(1),
                    "file": str(f.relative_to(repo_root)),
                })
        # TypeORM / Mongoose
        for f in _iter_files(repo_root, exts={".ts"}, max_files=300):
            content = _safe_read(f, max_bytes=3000)
            if "@Entity()" in content or "TypeOrmModule" in content:
                schema["orm"] = "typeorm"
            elif "mongoose.model(" in content or "new Schema(" in content:
                schema["orm"] = "mongoose"
            if "@Entity()" in content:
                for m in re.finditer(r"class\s+(\w+)", content):
                    schema["entities"].append({
                        "class": m.group(1),
                        "file": str(f.relative_to(repo_root)),
                    })

    # Deduplicate
    schema["tables_detected"] = list(dict.fromkeys(schema["tables_detected"]))
    schema["migration_files"] = list(dict.fromkeys(schema["migration_files"]))[:20]
    schema["entities"] = schema["entities"][:30]
    return schema


# ─── Service Topology ─────────────────────────────────────────────────────────

def detect_service_topology(repo_root: Path, tech_stack: dict) -> dict:
    """
    Discover what databases, external APIs, message queues, caches, and
    other microservices this service depends on. Scans config files and
    source code for client instantiation patterns.
    """
    topology = {
        "databases": [],
        "external_apis": [],
        "message_queues": [],
        "caches": [],
        "other_services": [],
        "docker_services": [],
    }

    # ── Parse docker-compose.yml ──
    for compose_file in ["docker-compose.yml", "docker-compose.yaml",
                          "docker-compose.dev.yml", "compose.yml"]:
        dc = repo_root / compose_file
        if dc.exists():
            content = _safe_read(dc)
            # Service names and images
            for m in re.finditer(r"image:\s*([^\s\n]+)", content):
                img = m.group(1).lower().split(":")[0].split("/")[-1]
                if any(db in img for db in ["postgres", "mysql", "mariadb", "mssql", "oracle", "mongodb", "cassandra"]):
                    topology["databases"].append(img)
                elif any(q in img for q in ["kafka", "rabbitmq", "activemq", "redis", "nats", "pulsar"]):
                    topology["message_queues"].append(img) if "kafka" in img or "rabbit" in img \
                        else topology["caches"].append(img) if "redis" in img \
                        else topology["message_queues"].append(img)
                else:
                    topology["other_services"].append(img)
            topology["docker_services"] = list(dict.fromkeys(
                topology["databases"] + topology["message_queues"] +
                topology["caches"] + topology["other_services"]
            ))

    # ── Scan config files ──
    config_files = []
    for pattern in ["**/*.yaml", "**/*.yml", "**/*.properties", "**/*.env", "**/*.toml",
                     "**/*.ini", "**/*.cfg", ".env", ".env.example", ".env.sample"]:
        for f in repo_root.glob(pattern):
            if not any(skip in str(f) for skip in SKIP_DIRS):
                config_files.append(f)

    combined_config = ""
    for f in config_files[:20]:
        combined_config += _safe_read(f, max_bytes=3000) + "\n"

    # Database connections
    db_patterns = [
        (r"(?:jdbc:|postgresql:|mysql:|mongodb[+]srv?:|redis://)([^\s'\"]+)", None),
        (r"(?:spring\.datasource\.url|DATABASE_URL|DB_HOST|POSTGRES_HOST|MYSQL_HOST)\s*[=:]\s*(\S+)", None),
        (r"(?:datasource|database).*(?:host|url)\s*[=:]\s*(\S+)", None),
    ]
    for pattern, _ in db_patterns:
        for m in re.finditer(pattern, combined_config, re.IGNORECASE):
            url = m.group(1).split("/")[0]
            if url and url not in topology["databases"]:
                topology["databases"].append(url[:80])

    # External API calls
    api_patterns = [
        r"(?:base_url|BASE_URL|API_URL|api_endpoint)\s*[=:]\s*['\"]?(https?://[^\s'\"]+)",
        r"(?:stripe\.com|twilio\.com|sendgrid|mailchimp|slack\.com|github\.com/api|"
        r"api\.openai|googleapis|amazonaws\.com|azure\.com)",
    ]
    for pattern in api_patterns:
        for m in re.finditer(pattern, combined_config, re.IGNORECASE):
            api = m.group(0)[:80]
            if api not in topology["external_apis"]:
                topology["external_apis"].append(api)

    # Message queues from config
    if any(k in combined_config.lower() for k in ["kafka.bootstrap", "spring.kafka", "kafka_brokers"]):
        if "kafka" not in topology["message_queues"]:
            topology["message_queues"].append("kafka")
    if any(k in combined_config.lower() for k in ["rabbitmq", "amqp://", "spring.rabbitmq"]):
        if "rabbitmq" not in topology["message_queues"]:
            topology["message_queues"].append("rabbitmq")
    if any(k in combined_config.lower() for k in ["sqs", "sns", "aws.sqs"]):
        if "aws-sqs" not in topology["message_queues"]:
            topology["message_queues"].append("aws-sqs/sns")

    # Caches
    if any(k in combined_config.lower() for k in ["redis.host", "spring.data.redis", "redis_url"]):
        if "redis" not in topology["caches"]:
            topology["caches"].append("redis")

    # ── Scan source for HTTP client calls to external services ──
    src_patterns = {
        "java": r"""(?:new\s+RestTemplate|WebClient\.builder|new\s+HttpClient|"https?://([^"]+)")""",
        "python": r"""requests\.(get|post|put|delete|patch)\s*\(\s*['"](https?://[^'"]+)""",
        "typescript": r"""(?:axios|fetch|http\w*)\s*\.\s*(?:get|post|put|delete)\s*\(\s*[`'"](https?://[^`'"]+)""",
        "javascript": r"""(?:axios|fetch)\s*\.\s*(?:get|post)\s*\(\s*['"](https?://[^'"]+)""",
        "go": r"""http\.(?:Get|Post)\s*\(\s*"(https?://[^"]+)""",
    }
    lang = tech_stack.get("language", "unknown")
    if lang in src_patterns:
        for f in _iter_files(repo_root, max_files=200):
            content = _safe_read(f, max_bytes=8000)
            for m in re.finditer(src_patterns[lang], content):
                url = m.group(m.lastindex or 1)[:80]
                if url and not any(x in url for x in ["localhost", "127.0.0.1", "{", "$"]):
                    if url not in topology["external_apis"]:
                        topology["external_apis"].append(url)

    # Deduplicate everything
    for key in topology:
        if isinstance(topology[key], list):
            topology[key] = list(dict.fromkeys(topology[key]))[:10]

    return topology


# ─── Module Graph (Upstream / Downstream) ─────────────────────────────────────

def build_module_graph(repo_root: Path, changed_files: list[str], tech_stack: dict) -> dict:
    """
    For each changed file:
      upstream   = files that this file imports (its dependencies)
      downstream = files that import this file (its consumers, blast radius)

    This tells agents:
    - Downstream count → how dangerous is this change (blast radius)
    - Upstream list → what this file depends on (context for understanding logic)
    - Downstream list → what files might be broken by this change
    """
    lang = tech_stack.get("language", "")
    graph = {}

    for rel_path in changed_files:
        file_path = repo_root / rel_path
        if not file_path.exists():
            continue

        content = _safe_read(file_path, max_bytes=20000)
        upstream = _extract_imports(content, rel_path, lang, repo_root)
        downstream = _find_dependents(rel_path, repo_root, lang)

        graph[rel_path] = {
            "upstream": upstream,        # what this file depends on
            "downstream": downstream,    # what depends on this file
            "blast_radius": len(downstream),
            "layer": _infer_layer(rel_path),
        }

    return graph


def _extract_imports(content: str, file_path: str, lang: str, repo_root: Path) -> list[str]:
    """Extract what a file imports, resolve to repo-relative paths where possible."""
    imports = []

    if lang in ("java", "kotlin"):
        for m in re.finditer(r"^import\s+([\w.]+);", content, re.MULTILINE):
            fqn = m.group(1)
            # Try to find the corresponding file in the repo
            rel = fqn.replace(".", "/") + ".java"
            candidates = [rel, rel.replace(".java", ".kt")]
            for candidate in candidates:
                if (repo_root / "src/main/java" / candidate).exists():
                    imports.append("src/main/java/" + candidate)
                    break
                if (repo_root / "src/main/kotlin" / candidate).exists():
                    imports.append("src/main/kotlin/" + candidate)
                    break
            else:
                imports.append(fqn)  # keep as FQN if file not found

    elif lang in ("typescript", "javascript"):
        for m in re.finditer(r"""(?:import|require)\s*(?:\{[^}]+\}|\w+)\s*from\s*['"]([^'"]+)['"]""", content):
            imp = m.group(1)
            if imp.startswith("."):
                # Relative import — resolve to file path
                base = Path(file_path).parent
                resolved = (base / imp).resolve()
                for ext in [".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.js"]:
                    candidate = str(resolved) + ext
                    rel = _try_make_relative(candidate, repo_root)
                    if rel:
                        imports.append(rel)
                        break
                else:
                    imports.append(str(resolved.relative_to(repo_root)) if resolved.is_relative_to(repo_root) else imp)
            elif not imp.startswith("@") or "/" in imp.lstrip("@"):
                imports.append(imp)  # keep as module name

    elif lang == "python":
        for m in re.finditer(r"^(?:from\s+([\w.]+)\s+import|import\s+([\w.,\s]+))", content, re.MULTILINE):
            module = (m.group(1) or m.group(2) or "").split(",")[0].strip()
            if module:
                # Try to find the file
                candidate = module.replace(".", "/") + ".py"
                if (repo_root / candidate).exists():
                    imports.append(candidate)
                else:
                    imports.append(module)

    elif lang == "go":
        for m in re.finditer(r'"([^"]+)"', content):
            pkg = m.group(1)
            if "/" in pkg and not pkg.startswith("github.com/"):
                imports.append(pkg)
            elif "/" in pkg:
                imports.append(pkg)

    return list(dict.fromkeys(imports))[:20]


def _find_dependents(target_path: str, repo_root: Path, lang: str) -> list[str]:
    """Find all files in the repo that import/reference the given file."""
    dependents = []
    stem = Path(target_path).stem
    name = Path(target_path).name

    # Search patterns by language
    if lang in ("java", "kotlin"):
        # Build the simple class name for grep
        search_term = stem
    elif lang in ("typescript", "javascript"):
        search_term = stem if stem != "index" else Path(target_path).parent.name
    elif lang == "python":
        search_term = stem
    elif lang == "go":
        search_term = stem
    else:
        return []

    # Use git grep for speed — falls back to manual scan
    try:
        result = subprocess.run(
            ["git", "grep", "-l", "--", search_term],
            cwd=repo_root,
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if line and line != target_path and not any(
                    skip in line for skip in ["test", "spec", "__tests__"]
                ):
                    dependents.append(line)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        # Fallback: manual scan of a limited set of files
        for f in _iter_files(repo_root, max_files=MAX_SCAN_FILES):
            rel = str(f.relative_to(repo_root))
            if rel == target_path:
                continue
            content = _safe_read(f, max_bytes=5000)
            if search_term in content:
                dependents.append(rel)

    return list(dict.fromkeys(dependents))[:15]


def _infer_layer(file_path: str) -> str:
    """Guess the architectural layer from the file path."""
    lower = file_path.lower()
    if any(x in lower for x in ["controller", "handler", "route", "resolver", "view"]):
        return "presentation"
    if any(x in lower for x in ["service", "usecase", "domain", "business"]):
        return "service"
    if any(x in lower for x in ["repository", "repo", "dao", "store", "persistence"]):
        return "data_access"
    if any(x in lower for x in ["model", "entity", "dto", "schema"]):
        return "model"
    if any(x in lower for x in ["config", "middleware", "filter", "interceptor", "gateway"]):
        return "infrastructure"
    if any(x in lower for x in ["test", "spec"]):
        return "test"
    if any(x in lower for x in ["util", "helper", "common", "shared"]):
        return "utility"
    return "unknown"


# ─── Test Coverage Map ────────────────────────────────────────────────────────

def build_test_coverage_map(repo_root: Path, changed_files: list[str], tech_stack: dict) -> dict:
    """
    For each changed source file, find test files that are likely to test it
    based on naming conventions.

    Convention patterns:
      Python:     foo/bar.py            → tests/foo/test_bar.py  (or test_foo/bar_test.py)
      Java:       src/main/.../Foo.java → src/test/.../FooTest.java
      Node:       src/foo/bar.ts        → src/foo/__tests__/bar.test.ts  (or bar.spec.ts)
      Go:         foo/bar.go            → foo/bar_test.go
      Ruby:       app/models/foo.rb     → spec/models/foo_spec.rb
    """
    coverage_map: dict[str, list[str]] = {}

    lang = tech_stack.get("language", "")

    # Pre-index test files for fast lookup
    test_files: dict[str, list[str]] = defaultdict(list)  # stem → [paths]
    for f in _iter_files(repo_root, max_files=500):
        rel = str(f.relative_to(repo_root))
        if any(x in rel.lower() for x in ["test", "spec", "__tests__"]):
            test_files[f.stem.lower()].append(rel)

    for source_path in changed_files:
        p = Path(source_path)
        stem = p.stem.lower()

        # Skip test files themselves
        if any(x in source_path.lower() for x in ["test", "spec", "__tests__"]):
            coverage_map[source_path] = []
            continue

        candidates = []

        # Language-specific conventions
        if lang in ("java", "kotlin"):
            test_stem = stem + "test"
            candidates += test_files.get(test_stem, [])
            candidates += test_files.get(stem + "tests", [])
            candidates += test_files.get(stem + "it", [])  # integration test
        elif lang == "python":
            candidates += test_files.get("test_" + stem, [])
            candidates += test_files.get(stem + "_test", [])
        elif lang in ("typescript", "javascript"):
            candidates += test_files.get(stem + ".test", [])
            candidates += test_files.get(stem + ".spec", [])
            candidates += test_files.get(stem, [f for f in test_files.get(stem, [])
                                                 if "test" in f or "spec" in f])
        elif lang == "go":
            candidates += [str(p.parent / (stem + "_test.go"))]
        elif lang == "ruby":
            candidates += test_files.get(stem + "_spec", [])
            candidates += test_files.get(stem + "_test", [])

        # Generic fallback — any test file containing the stem
        if not candidates:
            for test_stem_key, paths in test_files.items():
                if stem in test_stem_key or test_stem_key in stem:
                    candidates.extend(paths)

        # Verify candidates actually exist
        verified = [c for c in candidates if (repo_root / c).exists()]
        coverage_map[source_path] = list(dict.fromkeys(verified))[:5]

    return coverage_map


# ─── Main Builder ──────────────────────────────────────────────────────────────

def build_project_context(repo_root: Path, changed_files: list[str]) -> dict:
    """
    Full project context scan. Called once per PR review.
    Returns a dict that gets merged into the `knowledge` dict and passed to all agents.
    """
    print("[ContextBuilder] Scanning project architecture...")
    repo_root = repo_root.resolve()

    tech_stack = detect_tech_stack(repo_root)
    print(f"[ContextBuilder] Tech stack: {tech_stack['language']} / {tech_stack['framework']} / {tech_stack['build_tool']}")

    dependencies = parse_dependencies(repo_root, tech_stack)
    print(f"[ContextBuilder] Dependencies: {dependencies['total_count']} packages, "
          f"{len(dependencies['security_relevant'])} security-relevant")

    directory_map = build_directory_map(repo_root)
    print(f"[ContextBuilder] Directory map: {len(directory_map)} directories mapped")

    entry_points = find_entry_points(repo_root, tech_stack)
    print(f"[ContextBuilder] Entry points: {len(entry_points)} found")

    api_contracts = find_api_contracts(repo_root, tech_stack)
    print(f"[ContextBuilder] API contracts: {api_contracts['endpoint_count']} endpoints, "
          f"{len(api_contracts['openapi_files'])} OpenAPI files, "
          f"{len(api_contracts['proto_files'])} proto files")

    db_schema = find_db_schema(repo_root, tech_stack)
    print(f"[ContextBuilder] DB schema: {len(db_schema['entities'])} entities, "
          f"{len(db_schema['migration_files'])} migration files")

    service_topology = detect_service_topology(repo_root, tech_stack)
    print(f"[ContextBuilder] Service topology: "
          f"DBs={service_topology['databases']}, "
          f"Queues={service_topology['message_queues']}, "
          f"Caches={service_topology['caches']}")

    module_graph = build_module_graph(repo_root, changed_files, tech_stack)
    total_blast = sum(v["blast_radius"] for v in module_graph.values())
    print(f"[ContextBuilder] Module graph: {len(module_graph)} files mapped, "
          f"blast radius = {total_blast} downstream consumers")

    test_coverage_map = build_test_coverage_map(repo_root, changed_files, tech_stack)
    files_with_tests = sum(1 for v in test_coverage_map.values() if v)
    print(f"[ContextBuilder] Test coverage: {files_with_tests}/{len(changed_files)} "
          f"changed files have test coverage")

    return {
        "tech_stack": tech_stack,
        "dependencies": dependencies,
        "directory_map": directory_map,
        "entry_points": entry_points,
        "api_contracts": api_contracts,
        "db_schema": db_schema,
        "service_topology": service_topology,
        "module_graph": module_graph,
        "test_coverage_map": test_coverage_map,
    }


# ─── Utilities ────────────────────────────────────────────────────────────────

def _safe_read(path: Path, max_bytes: int = MAX_FILE_SIZE) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read(max_bytes)
    except (OSError, PermissionError):
        return ""


def _iter_files(root: Path, exts: set | None = None, max_files: int = MAX_SCAN_FILES):
    """Yield files under root, skipping SKIP_DIRS, optionally filtered by extension."""
    count = 0
    if not root.exists():
        return
    for f in root.rglob("*"):
        if count >= max_files:
            break
        if f.is_file() and not any(skip in f.parts for skip in SKIP_DIRS):
            if exts is None or f.suffix.lower() in exts:
                if f.stat().st_size <= MAX_FILE_SIZE:
                    yield f
                    count += 1


def _iter_dirs(root: Path, max_depth: int = 4):
    """Yield directories under root up to max_depth levels deep, skipping SKIP_DIRS."""
    if not root.exists():
        return
    for item in root.rglob("*/"):
        if not item.is_dir():
            continue
        if any(skip in item.parts for skip in SKIP_DIRS):
            continue
        depth = len(item.relative_to(root).parts)
        if depth <= max_depth:
            yield item


def _try_make_relative(path_str: str, repo_root: Path) -> str | None:
    try:
        p = Path(path_str)
        if p.exists():
            return str(p.relative_to(repo_root))
    except (ValueError, OSError):
        pass
    return None
