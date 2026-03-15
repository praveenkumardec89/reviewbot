"""
Architecture Agent — design patterns, API contracts, coupling, SOLID principles.
Runs on larger PRs, new files, or API-touching changes.
"""

from pathlib import Path
from .base import BaseAgent, COMMENT_FORMAT

API_INDICATORS = {"api", "route", "handler", "controller", "endpoint", "router", "service", "interface"}


class ArchitectureAgent(BaseAgent):
    name = "architecture"
    description = "design patterns, API contracts, SOLID principles, coupling, dependency design"
    emoji = "🏗️"

    def should_run(self, pr_context: dict) -> tuple[bool, str]:
        additions = pr_context.get("total_additions", 0)
        file_count = pr_context.get("file_count", 0)
        file_names = pr_context.get("file_names", [])

        # High blast radius — changing a file many things depend on
        module_graph = pr_context.get("project_context", {}).get("module_graph", {})
        max_blast = max((v.get("blast_radius", 0) for v in module_graph.values()), default=0)
        if max_blast >= 3:
            return True, f"high blast radius — up to {max_blast} downstream dependents affected"

        # Service/controller layer changes detected via directory map
        dir_map = pr_context.get("project_context", {}).get("directory_map", {})
        if dir_map:
            for fname in file_names:
                parent = str(Path(fname).parent)
                purpose = dir_map.get(parent, "")
                if any(x in purpose for x in ["service", "controller", "entry point", "gateway", "adapter"]):
                    return True, f"service/API layer changed: {fname}"

        # Large PR
        if additions > 150 or file_count >= 8:
            return True, f"large PR ({additions} additions, {file_count} files)"

        # New files added
        has_new_files = pr_context.get("has_new_files", False)
        if has_new_files:
            return True, "new files introduced"

        # API/interface-touching changes
        for name in file_names:
            lower = name.lower()
            if any(ind in lower for ind in API_INDICATORS):
                return True, f"API/service layer file changed: {Path(name).name}"

        # Config/infra changes that affect architecture
        if pr_context.get("has_config") and additions > 20:
            return True, "significant config changes"

        return False, f"PR too small ({additions} additions) and no API files — skipping architecture review"

    def build_system_prompt(self, knowledge: dict) -> str:
        infra = knowledge.get("infra", {})
        infra_text = ""
        if infra:
            infra_text = "\nREPO COMPONENT KNOWLEDGE:\n"
            for component, info in list(infra.items())[:8]:
                infra_text += f"  {component}:\n"
                if isinstance(info, dict):
                    for k, v in info.items():
                        infra_text += f"    {k}: {v}\n"

        # Add tech stack context for framework-specific advice
        ctx = knowledge.get("project_context", {})
        ts = ctx.get("tech_stack", {})
        stack_text = ""
        if ts.get("framework") and ts["framework"] != "unknown":
            stack_text = (
                f"\nPROJECT TECH STACK: {ts.get('language', '')} / {ts.get('framework', '')} "
                f"/ {ts.get('build_tool', '')}\n"
                "Apply framework-specific architectural best practices. "
                f"E.g. for Spring Boot: check @Service/@Repository/@Controller layering, "
                f"dependency injection, avoid @Autowired on fields. "
                f"For Django: check fat-model vs fat-view, signals misuse. "
                f"For NestJS: check module boundaries, circular injection."
            )

        # Topology context — understanding cross-service impact
        topo = ctx.get("service_topology", {})
        topology_text = ""
        if topo.get("message_queues") or topo.get("external_apis"):
            topology_text = "\nSERVICE DEPENDENCIES THIS REPO HAS:\n"
            if topo.get("databases"):
                topology_text += f"  Databases: {', '.join(topo['databases'][:5])}\n"
            if topo.get("message_queues"):
                topology_text += f"  Message queues: {', '.join(topo['message_queues'][:5])}\n"
            if topo.get("external_apis"):
                topology_text += f"  External APIs: {', '.join(topo['external_apis'][:5])}\n"
            topology_text += ("Be extra careful about changes that affect message contracts, "
                               "API versioning, or DB schema compatibility across services.\n")

        # Team's explicit architectural rules from architecture.yaml
        arch_impact = ctx.get("arch_impact", {})
        arch_config = ctx.get("arch_config", {})

        layer_rules_text = ""
        layers = arch_config.get("layers", {})
        if layers:
            layer_rules_text = "\nTEAM LAYER ARCHITECTURE (from .reviewcrew/architecture.yaml):\n"
            for layer_name, layer_cfg in list(layers.items())[:6]:
                if not isinstance(layer_cfg, dict):
                    continue
                desc = layer_cfg.get("description", "")
                forbidden = layer_cfg.get("forbidden_deps", [])
                layer_rules_text += f"  {layer_name}: {desc}\n"
                if forbidden:
                    layer_rules_text += f"    forbidden imports from: {', '.join(forbidden)}\n"
                notes = layer_cfg.get("notes", "")
                if notes:
                    layer_rules_text += f"    notes: {notes}\n"

        pre_detected_violations_text = ""
        violations = arch_impact.get("layer_violations", [])
        if violations:
            pre_detected_violations_text = "\nPRE-DETECTED LAYER VIOLATIONS IN THIS PR:\n"
            for v in violations[:5]:
                pre_detected_violations_text += f"  - {v['reason']}\n"
            pre_detected_violations_text += (
                "These are definite violations — flag each one as a finding.\n"
            )

        upstream_text = ""
        upstream_impact = arch_impact.get("upstream_impact", [])
        if upstream_impact:
            upstream_text = "\nUPSTREAM SERVICES THAT MAY BE AFFECTED:\n"
            for u in upstream_impact[:4]:
                upstream_text += (
                    f"  - {u['service']}: {u.get('description','')} "
                    f"(severity: {u.get('breaking_change_severity','high')})\n"
                    f"    Changed files: {', '.join(u.get('changed_files',[])[:3])}\n"
                )
            upstream_text += ("Check these changes for breaking contract changes. "
                               "Flag if API shapes, status codes, or schemas were altered.\n")

        downstream_text = ""
        downstream_impact = arch_impact.get("downstream_impact", [])
        if downstream_impact:
            downstream_text = "\nDOWNSTREAM INTEGRATIONS CHANGED:\n"
            for d in downstream_impact[:4]:
                downstream_text += (
                    f"  - {d['service']} ({d.get('type','rest')}): {d.get('description','')}\n"
                    f"    Changed files: {', '.join(d.get('changed_files',[])[:3])}\n"
                )

        event_text = ""
        event_impact = arch_impact.get("event_impact", [])
        if event_impact:
            event_text = "\nEVENT CONTRACTS TOUCHED:\n"
            for e in event_impact[:5]:
                if e["type"] == "publishes":
                    event_text += (
                        f"  - Published event `{e['topic']}` schema file changed. "
                        f"Consumers: {', '.join(e.get('consumers',[]))}. "
                        f"This is a {e.get('breaking_change_severity','critical')} risk.\n"
                    )
                else:
                    event_text += (
                        f"  - Consumer for `{e['topic']}` from {e.get('from','')} was changed.\n"
                    )

        custom_rules_text = ""
        custom_rule_hits = arch_impact.get("custom_rule_hits", [])
        if custom_rule_hits:
            custom_rules_text = "\nTEAM CUSTOM RULES TO ENFORCE ON CHANGED FILES:\n"
            for r in custom_rule_hits[:8]:
                custom_rules_text += (
                    f"  [{r['severity'].upper()}] {r['rule_id']}: {r['description']}\n"
                    f"    applies to: {', '.join(f'`{f}`' for f in r['matched_files'][:3])}\n"
                )
            custom_rules_text += "Flag any violations of these rules as findings.\n"

        return f"""You are a principal software architect performing a structural design review.
{stack_text}{topology_text}{layer_rules_text}{pre_detected_violations_text}{upstream_text}{downstream_text}{event_text}{custom_rules_text}

YOUR SOLE FOCUS — flag only these categories:

1. SOLID VIOLATIONS:
   - Single Responsibility: class/module doing too many unrelated things
   - Open/Closed: hardcoded conditionals that should be polymorphism/strategy
   - Liskov: subclass that breaks parent contract
   - Interface Segregation: fat interface where callers only need a subset
   - Dependency Inversion: high-level module directly instantiating low-level dependencies (use DI)

2. API & CONTRACT DESIGN:
   - Breaking changes to public APIs without versioning or deprecation path
   - Inconsistent response shapes or status codes in the same API surface
   - Missing pagination on list endpoints that will grow
   - Leaking internal implementation details through public API shapes

3. COUPLING & COHESION:
   - Circular dependencies between modules/packages
   - Direct DB calls in presentation layer (controller/handler)
   - Business logic embedded in infrastructure layer
   - Cross-cutting concerns (logging, auth, metrics) not centralized

4. SCALABILITY & EXTENSIBILITY:
   - Hardcoded limits or assumptions that won't hold at scale
   - Configuration that should be data-driven hardcoded in logic
   - Missing abstraction layer that would make this easily testable/replaceable

5. DEPENDENCY MANAGEMENT:
   - New heavy dependency introduced for a trivial use case
   - Mixing abstraction levels (e.g., raw HTTP in domain logic)
{infra_text}
IMPORTANT:
- Do NOT flag micro-level code issues (that's code_quality's job)
- Think in terms of long-term maintainability and team scalability
- When suggesting a fix, show the structural change, not just "use a pattern"
- Praise genuinely good architectural decisions

{COMMENT_FORMAT}"""
