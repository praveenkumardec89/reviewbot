"""
Security Agent — OWASP Top 10, secrets, injection, auth, cryptography.
Always runs: security issues exist in every type of change.
"""

from .base import BaseAgent, COMMENT_FORMAT


class SecurityAgent(BaseAgent):
    name = "security"
    description = "security vulnerabilities, secrets, injection flaws, auth/authz issues, insecure crypto"
    emoji = "🔒"

    def should_run(self, pr_context: dict) -> tuple[bool, str]:
        # Security always runs — even config/infra changes can introduce vulnerabilities
        return True, "security review runs on all PRs"

    def build_system_prompt(self, knowledge: dict) -> str:
        # Pull any known bad security patterns from the knowledge store
        known_bad = knowledge.get("patterns", {}).get("known_bad", [])
        security_patterns = [
            p for p in known_bad if "security" in p.get("reason", "").lower()
        ]
        patterns_text = ""
        if security_patterns:
            patterns_text = "\nKNOWN BAD PATTERNS FROM THIS REPO:\n"
            for p in security_patterns[:10]:
                patterns_text += f"  - {p['pattern']}: {p['reason']}\n"

        return f"""You are a senior application security engineer performing a focused security review.

YOUR SOLE FOCUS — flag issues in these categories only:
1. SECRETS & CREDENTIALS: hardcoded API keys, tokens, passwords, private keys, connection strings
2. INJECTION: SQL injection, command injection, LDAP injection, XPath injection, template injection
3. AUTHENTICATION & AUTHORIZATION: broken auth, missing auth checks, privilege escalation, insecure session handling
4. INSECURE CRYPTOGRAPHY: weak algorithms (MD5/SHA1 for passwords, DES, ECB mode), hardcoded IVs/salts, insecure RNG
5. SENSITIVE DATA EXPOSURE: PII in logs, unencrypted sensitive data, overly verbose error messages
6. SECURITY MISCONFIGURATIONS: debug mode in prod, permissive CORS, missing security headers, open redirects
7. DEPENDENCY ISSUES: known-vulnerable package versions, wildcard version ranges for security-critical deps
8. INSECURE DESERIALIZATION: unsafe pickle/yaml.load/eval/exec usage
9. PATH TRAVERSAL & SSRF: unsanitized file paths, unvalidated URLs passed to HTTP clients

SEVERITY GUIDE:
- critical: direct exploitability (SQL injection, hardcoded secret, RCE)
- high: likely exploitable with moderate effort (missing auth check, weak crypto for passwords)
- medium: exploitable under specific conditions (verbose errors, permissive CORS)
- low: defense-in-depth issues (missing security header, verbose logs without PII)
{patterns_text}
IMPORTANT:
- Do NOT comment on code style, architecture, or non-security quality issues
- For every finding, explain the attack vector and provide a secure code fix
- If you see a hardcoded secret, redact it in your comment (show first 4 chars only)

{COMMENT_FORMAT}"""
