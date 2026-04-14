"""
Port PR Readiness Review Agent  (OpenAI version)
--------------------------------------------------
Port calls this webhook when the ai_pr_review_on_created automation fires.
Fetches service context from Port, reasons with GPT-4o, posts a GitHub
comment, and writes verdict back to the Port PR entity.
Works for any GitHub repo and any Port service — fully dynamic.
"""

import os
import json
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

PORT_CLIENT_ID     = os.environ.get("PORT_CLIENT_ID", "")
PORT_CLIENT_SECRET = os.environ.get("PORT_CLIENT_SECRET", "")
GITHUB_TOKEN       = os.environ.get("GITHUB_TOKEN", "")
OPENAI_API_KEY     = os.environ.get("OPENAI_API_KEY", "")

PORT_API_BASE  = "https://api.getport.io/v1"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL   = "gpt-4o"


# ── Port auth ─────────────────────────────────────────────────────────────────
def get_port_token() -> str:
    resp = requests.post(
        f"{PORT_API_BASE}/auth/access_token",
        json={"clientId": PORT_CLIENT_ID, "clientSecret": PORT_CLIENT_SECRET},
    )
    resp.raise_for_status()
    return resp.json()["accessToken"]


# ── Fetch service context from Port ───────────────────────────────────────────
def fetch_service_context(service_id: str, token: str) -> dict:
    headers = {"Authorization": f"Bearer {token}"}

    # Single API call — entity includes properties, team, AND scorecards
    svc_resp = requests.get(
        f"{PORT_API_BASE}/blueprints/service/entities/{service_id}",
        headers=headers,
    )
    svc_resp.raise_for_status()
    svc = svc_resp.json()["entity"]

    # Scorecard is inside the entity response — no extra API call needed
    scorecard = svc.get("scorecards", {}).get("ProductionReadinessGithubOcean", {})

    # Fetch ALL workloads then filter by service in Python
    wl_resp = requests.get(
        f"{PORT_API_BASE}/blueprints/workload/entities",
        headers=headers,
    )
    all_workloads = wl_resp.json().get("entities", []) if wl_resp.ok else []

    # Filter workloads belonging to this service — handle both dict and string relation
    workloads = [
        w for w in all_workloads
        if (
            isinstance(w.get("relations", {}).get("service"), dict)
            and w["relations"]["service"].get("identifier") == service_id
        ) or w.get("relations", {}).get("service") == service_id
    ]

    return {
        "identifier": svc.get("identifier"),
        "title":      svc.get("title"),
        "team":       svc.get("team", []),
        "properties": svc.get("properties", {}),   # readme, codeowners, last_push
        "scorecard":  scorecard,
        "workloads": [
            {
                "title":       w.get("title"),
                "version":     w.get("properties", {}).get("version"),
                "environment": (
                    w.get("relations", {}).get("environment", {}).get("identifier")
                    if isinstance(w.get("relations", {}).get("environment"), dict)
                    else w.get("relations", {}).get("environment")
                ),
            }
            for w in workloads
        ],
    }


# ── Reason with OpenAI GPT-4o ─────────────────────────────────────────────────
def reason_with_llm(pr_context: dict, svc: dict) -> dict:
    sc      = svc.get("scorecard", {})
    level   = sc.get("level", "Unknown")
    rules   = sc.get("rules", [])
    passing = [r["identifier"] for r in rules if r.get("status") == "SUCCESS"]
    failing = [r["identifier"] for r in rules if r.get("status") != "SUCCESS"]
    wls     = svc.get("workloads", [])
    wl_str  = (
        ", ".join(
            f"{w['title']} ({w.get('environment', '?')})"
            for w in wls
        )
        if wls else "No active workloads found"
    )

    props = svc.get("properties", {})

    system_prompt = """You are a senior platform engineer reviewing a pull request.
You receive context from Port's internal developer portal (the context lake).
Be direct and specific. Return ONLY valid JSON — no markdown fences, no preamble.

JSON shape:
{
  "verdict": "APPROVED" | "REVIEW NEEDED" | "BLOCKED",
  "risk_level": "Low" | "Medium" | "High" | "Critical",
  "summary": "2-3 sentence summary",
  "action_items": ["item1", "item2"],
  "github_comment_markdown": "Full markdown comment to post on the PR"
}

For github_comment_markdown, use a table with three rows (Ownership, Scorecard, Deployment),
a bold Verdict line, and a checkbox list of action items."""

    user_msg = f"""
PR:
- Repo:   {pr_context['repo_name']}
- PR #:   {pr_context['pr_number']}
- Branch: {pr_context['branch']}

Service context from Port:
- Service:            {svc['title']} ({svc['identifier']})
- Owning team:        {', '.join(svc.get('team', [])) or 'NOT SET'}
- Scorecard level:    {level}  (F is worst, A is best)
- Passing rules:      {', '.join(passing) or 'None'}
- Failing rules:      {', '.join(failing) or 'None'}
- README present:     {bool(props.get('readme'))}
- CODEOWNERS present: {bool(props.get('codeowners'))}
- Last push:          {props.get('last_push', 'unknown')}
- Active workloads:   {wl_str}
"""

    resp = requests.post(
        OPENAI_API_URL,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type":  "application/json",
        },
        json={
            "model":       OPENAI_MODEL,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_msg},
            ],
        },
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"].strip()

    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    return json.loads(raw)


# ── Post GitHub PR comment ────────────────────────────────────────────────────
def post_github_comment(repo_name: str, pr_number: int, body: str) -> bool:
    resp = requests.post(
        f"https://api.github.com/repos/{repo_name}/issues/{pr_number}/comments",
        headers={
            "Authorization":        f"Bearer {GITHUB_TOKEN}",
            "Accept":               "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"body": body},
    )
    return resp.ok


# ── Update Port action run ────────────────────────────────────────────────────
def update_port_run(run_id: str, token: str, verdict: str, summary: str, success: bool):
    if not run_id:
        return
    requests.patch(
        f"{PORT_API_BASE}/actions/runs/{run_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "status":  "SUCCESS" if success else "FAILURE",
            "summary": f"[{verdict}] {summary}",
        },
    )


# ── Write verdict back to Port PR entity ─────────────────────────────────────
def update_pr_verdict_in_port(entity_id: str, review: dict, token: str):
    if not entity_id:
        print("DEBUG → No entity_id, skipping Port verdict update")
        return

    verdict_map = {
        "APPROVED":      "🟢 Low",
        "REVIEW NEEDED": "🟡 Medium",
        "BLOCKED":       "🔴 High",
    }
    risk = verdict_map.get(review.get("verdict", ""), "🟡 Medium")

    resp = requests.patch(
        f"{PORT_API_BASE}/blueprints/githubPullRequest/entities/{entity_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        },
        json={
            "properties": {
                "ai_risk_level": risk,
                "ai_summary":    review.get("summary", ""),
            },
        },
    )
    if resp.ok:
        print(f"DEBUG → Port PR verdict updated: {risk}")
    else:
        print(f"DEBUG → Port verdict update failed: {resp.status_code} {resp.text}")


# ── Core review logic ─────────────────────────────────────────────────────────
def run_review(service_id: str, pr_context: dict, run_id: str = None,
               port_entity_id: str = None):
    token  = get_port_token()
    svc    = fetch_service_context(service_id, token)
    review = reason_with_llm(pr_context, svc)

    gh_ok  = post_github_comment(
                 pr_context["repo_name"],
                 pr_context["pr_number"],
                 review.get("github_comment_markdown", "Port PR review complete.")
             )
    print(f"DEBUG → GitHub comment posted: {gh_ok}")

    if port_entity_id:
        update_pr_verdict_in_port(port_entity_id, review, token)

    update_port_run(
        run_id, token,
        verdict=review.get("verdict", "UNKNOWN"),
        summary=review.get("summary", ""),
        success=gh_ok,
    )
    return review, gh_ok


# ── Webhook endpoint ──────────────────────────────────────────────────────────
@app.route("/webhook/pr-review", methods=["POST"])
def pr_review_webhook():
    payload = request.get_json(force=True)
    print("DEBUG PAYLOAD:", json.dumps(payload, indent=2))

    run_id    = payload.get("run_id")
    repo_name = payload.get("repo_name")
    pr_number = int(payload.get("pr_number") or 0)
    pr_url    = payload.get("pr_url", "")
    branch    = payload.get("branch", "unknown")
    entity_id = payload.get("entity_identifier")

    # Handle service_identifier — Port can send string or dict
    service_raw = payload.get("service_identifier")
    if isinstance(service_raw, dict):
        service_id = service_raw.get("identifier")
    elif isinstance(service_raw, str) and service_raw:
        service_id = service_raw
    else:
        service_id = None

    print(f"DEBUG → service={service_id} pr={pr_number} repo={repo_name} entity={entity_id}")

    if not service_id:
        print("ERROR → service_identifier is None")
        return jsonify({"status": "error", "message": "service_identifier is missing"}), 400

    pr_context = {
        "repo_name": repo_name,
        "pr_number": pr_number,
        "pr_url":    pr_url,
        "branch":    branch,
    }

    try:
        review, gh_ok = run_review(service_id, pr_context, run_id, entity_id)
        return jsonify({
            "status":                "ok",
            "verdict":               review.get("verdict"),
            "risk_level":            review.get("risk_level"),
            "github_comment_posted": gh_ok,
        }), 200

    except Exception as e:
        print(f"ERROR: {e}")
        import traceback; traceback.print_exc()
        if run_id:
            try:
                t = get_port_token()
                update_port_run(run_id, t, "ERROR", str(e), False)
            except Exception:
                pass
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"}), 200


if __name__ == "__main__":
    print("Server running on http://0.0.0.0:8080")
    app.run(host="0.0.0.0", port=8080, debug=False)