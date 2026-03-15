"""
Performance Agent — N+1 queries, algorithmic complexity, memory leaks, caching.
Runs only when performance-sensitive patterns are detected.
"""

from .base import BaseAgent, COMMENT_FORMAT

PERF_INDICATORS = {
    # DB/ORM patterns
    "query", "select", "insert", "update", "delete", "join", "fetch",
    "find_all", "find_by", "where", "filter", "objects.all", "prisma",
    "mongoose", "sequelize", "sqlalchemy", "hibernate", "repository",
    # Loop/data patterns
    "for ", "foreach", "while ", ".map(", ".filter(", ".reduce(",
    "sort(", "sorted(", "order_by",
    # Heavy compute
    "json.loads", "json.dumps", "serialize", "deserialize", "parse",
    # File/network I/O in loops
    "requests.get", "fetch(", "http.", "urllib", "open(",
    # Caching
    "cache", "redis", "memcache", "lru_cache",
}

PERF_FILE_PATTERNS = {
    "migration", "model", "entity", "repository", "dao", "service",
    "worker", "job", "task", "queue", "pipeline", "processor",
}


class PerformanceAgent(BaseAgent):
    name = "performance"
    description = "N+1 queries, algorithmic complexity, memory inefficiency, missing caching, blocking I/O"
    emoji = "⚡"

    def should_run(self, pr_context: dict) -> tuple[bool, str]:
        if not pr_context.get("has_code"):
            return False, "no code files"

        file_names = pr_context.get("file_names", [])
        diff = pr_context.get("diff", "").lower()

        # Check file names for performance-sensitive patterns
        for fname in file_names:
            lower = fname.lower()
            if any(p in lower for p in PERF_FILE_PATTERNS):
                return True, f"performance-sensitive file: {fname}"

        # Check diff content for performance-sensitive code patterns
        matched = [ind for ind in PERF_INDICATORS if ind in diff]
        if len(matched) >= 3:
            return True, f"performance-sensitive patterns in diff: {', '.join(matched[:3])}"

        # Large additions with loops or queries
        if pr_context.get("total_additions", 0) > 100 and any(
            ind in diff for ind in ["for ", "while ", "query", "select"]
        ):
            return True, "large addition with loops/queries detected"

        if pr_context.get("has_sql"):
            return True, "SQL/migration files present"

        return False, "no performance-sensitive patterns detected"

    def build_system_prompt(self, knowledge: dict) -> str:
        infra = knowledge.get("infra", {})
        hotspots = {
            k: v for k, v in infra.items()
            if isinstance(v, dict) and v.get("quality_hotspot")
        }
        hotspot_text = ""
        if hotspots:
            hotspot_text = "\nKNOWN PERFORMANCE HOTSPOTS IN THIS REPO:\n"
            for component, info in hotspots.items():
                hotspot_text += f"  {component}: {info.get('recent_critical_issues', 0)} recent issues\n"

        ctx = knowledge.get("project_context", {})
        topo = ctx.get("service_topology", {})
        topo_text = ""
        if topo.get("databases") or topo.get("message_queues") or topo.get("caches"):
            topo_text = "\nINFRASTRUCTURE THIS SERVICE USES:\n"
            if topo.get("databases"):
                topo_text += f"  Databases: {', '.join(topo['databases'][:4])}\n"
            if topo.get("caches"):
                topo_text += f"  Caches available: {', '.join(topo['caches'][:3])}\n"
                topo_text += ("  When flagging missing caching, mention the available cache layer above.\n")
            if topo.get("message_queues"):
                topo_text += f"  Message queues: {', '.join(topo['message_queues'][:3])}\n"
            if not topo.get("caches"):
                topo_text += ("  No caching layer detected — be realistic about suggesting cache solutions "
                               "without first recommending adding a cache infrastructure.\n")

        db = ctx.get("db_schema", {})
        schema_text = ""
        if db.get("orm") and db["orm"] != "unknown":
            schema_text = f"\nORM IN USE: {db['orm']}\n"
            if db.get("entities"):
                entity_names = [e["class"] for e in db["entities"][:10]]
                schema_text += f"  Entities: {', '.join(entity_names)}\n"
            schema_text += ("  Check for N+1 queries with lazy-loaded associations. "
                             "Suggest eager loading / JOIN FETCH where appropriate.\n")

        return f"""You are a performance engineering specialist reviewing code for efficiency issues.
{topo_text}{schema_text}

YOUR SOLE FOCUS — flag only these categories:

1. N+1 QUERY PROBLEMS:
   - Database/API calls inside loops (fetch in loop pattern)
   - ORM lazy loading that triggers a query per iteration
   - Missing eager loading / JOIN where N separate queries are made
   - Suggest: bulk fetch before loop, eager loading, or JOIN

2. ALGORITHMIC COMPLEXITY:
   - O(n²) or worse where O(n log n) or O(n) is achievable
   - Nested loops over the same collection
   - Linear search in a hot path where a hash map / set would be O(1)
   - Sorting inside a loop (sort once outside)

3. MEMORY INEFFICIENCY:
   - Loading entire large dataset into memory when streaming would work
   - Accumulating results in a list when they're consumed once (use generator)
   - Not paginating large result sets
   - Creating large intermediate collections unnecessarily

4. MISSING CACHING:
   - Repeated expensive computation with same inputs (pure function not memoized)
   - Repeated DB reads of rarely-changing data without caching
   - Missing HTTP caching headers on expensive endpoints

5. BLOCKING I/O IN ASYNC CONTEXTS:
   - Synchronous I/O (file read, HTTP call) in async/event-loop code
   - Missing `async/await` on I/O operations in async functions
   - Thread-blocking calls in a single-threaded event loop

6. RESOURCE MANAGEMENT:
   - DB connections not properly pooled or released
   - File handles opened but not closed (missing context manager)
   - Goroutine/thread leaks (spawned but never joined/cancelled)
{hotspot_text}
IMPORTANT:
- Only flag issues with measurable impact — don't flag micro-optimizations
- Quantify the issue when possible ("this executes N+1 queries where N = number of users")
- Show the optimized version, not just "this is slow"
- Don't flag premature optimization of code that runs once at startup

{COMMENT_FORMAT}"""
