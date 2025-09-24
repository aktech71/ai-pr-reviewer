from flask import Flask, request, jsonify
import os, time, jwt, requests, openai

app = Flask(__name__)

# Load config from environment
APP_ID = os.environ['GITHUB_APP_ID']
INSTALLATION_ID = os.environ['GITHUB_INSTALLATION_ID']
PRIVATE_KEY = os.environ['GITHUB_PRIVATE_KEY']
OPENAI_KEY = os.environ['OPENAI_API_KEY']
openai.api_key = OPENAI_KEY

def create_jwt():
    """Generate a GitHub App JWT (Bearer) for authentication [oai_citation:10‡docs.github.com](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app#:~:text=Your%20JWT%20must%20be%20signed,must%20contain%20the%20following%20claims)."""
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + (10*60), "iss": APP_ID}
    token = jwt.encode(payload, PRIVATE_KEY, algorithm='RS256')
    return token

def get_install_token():
    """Exchange App JWT for an installation access token (to call GitHub API)."""
    jwt_token = create_jwt()
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json"
    }
    url = f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    res = requests.post(url, headers=headers)
    res.raise_for_status()
    return res.json()['token']

@app.route("/webhook", methods=["POST"])
def handle_webhook():
    event = request.headers.get('X-GitHub-Event', '')
    payload = request.json

    # Only handle Pull Request events (opened or updated)
    if event == "pull_request" and payload.get("action") in ["opened", "synchronize", "edited"]:
        owner = payload["repository"]["owner"]["login"]
        repo  = payload["repository"]["name"]
        pr_number = payload["pull_request"]["number"]
        head_sha  = payload["pull_request"]["head"]["sha"]

        # Get installation token for GitHub API
        token = get_install_token()
        gh_headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

        # 1) Fetch the list of changed files in the PR
        files_url = payload["pull_request"]["url"] + "/files"
        files_res = requests.get(files_url, headers=gh_headers)
        files_res.raise_for_status()
        files = files_res.json()

        # 2) Build a unified diff string for summarization
        diff_text = ""
        for f in files:
            patch = f.get("patch")
            if patch:
                diff_text += f"File: {f['filename']}\n{patch}\n\n"

        # 3) Call GPT-4 to summarize the PR diff [oai_citation:11‡ericmjl.github.io](https://ericmjl.github.io/blog/2023/5/13/how-to-craft-stellar-pull-request-summaries-with-gpt-4/#:~:text=Here%27s%20the%20code%20for%20generating,the%20summary%20message)
        system_msg = {"role": "system", "content": "You are a helpful senior engineer."}
        user_msg = {
            "role": "user",
            "content": (
                f"Given the following pull request diff, summarize the key changes and benefits in plain English:\n\n{diff_text}"
            )
        }
        summary_resp = openai.ChatCompletion.create(
            model="gpt-4", messages=[system_msg, user_msg], temperature=0.2
        )
        pr_summary = summary_resp["choices"][0]["message"]["content"]

        # 4) Post the summary as a comment on the PR
        issues_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"
        comment_payload = {"body": f"**PR Summary:**\n\n{pr_summary}"}
        requests.post(issues_url, headers=gh_headers, json=comment_payload)

        # 5) Determine if this is a "low-risk" PR (simple check: few total changes)
        total_changes = sum(f.get("changes", 0) for f in files)
        low_risk = total_changes < 10  # e.g., less than 10 lines changed

        # 6) If low-risk, auto-approve; else generate inline comments
        reviews_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews"

        if low_risk:
            # Approve PR without comments
            approve_payload = {"event": "APPROVE"}
            requests.post(reviews_url, headers=gh_headers, json=approve_payload)
        else:
            comments = []
            for f in files:
                patch = f.get("patch")
                if not patch: 
                    continue
                # Use GPT-4 to critique this file's diff
                critique_msg = {
                    "role": "user",
                    "content": (
                        f"Review the following diff from file {f['filename']} and provide any comments "
                        "on improvements or issues, prefixed by the line number (e.g. 'Line 27: ...'):\n\n"
                        f"{patch}"
                    )
                }
                critique_resp = openai.ChatCompletion.create(
                    model="gpt-4", messages=[system_msg, critique_msg], temperature=0.3
                )
                critique = critique_resp["choices"][0]["message"]["content"]

                # Parse GPT-4 response for lines like "Line X: comment"
                for line in critique.splitlines():
                    if line.strip().startswith("Line"):
                        parts = line.split(":", 1)
                        if len(parts) < 2: continue
                        try:
                            line_no = int(parts[0].split()[1])
                        except ValueError:
                            continue
                        body = parts[1].strip()
                        comments.append({
                            "path": f["filename"],
                            "position": line_no,
                            "body": body
                        })

            # Post a review with all inline comments [oai_citation:12‡docs.github.com](https://docs.github.com/en/rest/pulls/comments#:~:text=Create%20a%20review%20comment%20for,a%20pull%20request) [oai_citation:13‡docs.github.com](https://docs.github.com/en/rest/pulls/comments#:~:text=%60curl%20,d%20%27%7B%22body%22%3A%22Great%20stuff%21%22%2C%22commit_id%22%3A%226dcb09b5b57875f334f61aebed695e2e4193db5e%22%2C%22%20path%22%3A%22file1.txt%22%2C%22start_line%22%3A1%2C%22start_side%22%3A%22RIGHT%22%2C%22line%22%3A2%2C%22side%22%3A%22RIGHT)
            if comments:
                review_payload = {"event": "COMMENT", "comments": comments}
                requests.post(reviews_url, headers=gh_headers, json=review_payload)

    return jsonify({'status': 'ok'}), 200

if __name__ == "__main__":
    app.run(port=5000)