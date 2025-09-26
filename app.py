from flask import Flask, request, jsonify
import os, requests
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()  # Load environment variables from .env file

app = Flask(__name__)

# Load config from environment variables
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]        # Your Personal Access Token
OPENAI_KEY = os.environ["OPENAI_API_KEY"]
client = OpenAI(api_key=OPENAI_KEY)

print("Starting webhook server...")
print("Listening for GitHub webhooks...")

@app.route("/", methods=["GET"])
def index():
    return "✅ Webhook server is running", 200

@app.route("/webhook", methods=["POST"])
def handle_webhook():
    event = request.headers.get("X-GitHub-Event", "")
    payload = request.json

    # Only handle pull_request events
    if event == "pull_request" and payload.get("action") in ["opened", "synchronize", "edited"]:
        owner = payload["repository"]["owner"]["login"]
        repo = payload["repository"]["name"]
        pr_number = payload["number"]  # top-level key for PR number
        head_sha = payload["pull_request"]["head"]["sha"]

        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json"
        }

        # 1) Get list of changed files
        files_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files"
        files_res = requests.get(files_url, headers=headers)
        files_res.raise_for_status()
        files = files_res.json()

        # 2) Build a unified diff string
        diff_text = ""
        for f in files:
            patch = f.get("patch")
            if patch:
                diff_text += f"File: {f['filename']}\n{patch}\n\n"

        # 3) Ask GPT-4 for a summary
        system_msg = {"role": "system", "content": "You are a helpful senior engineer."}
        user_msg = {
            "role": "user",
            "content": f"Summarize the following pull request diff in plain English:\n\n{diff_text}"
        }
        summary_resp = client.chat.completions.create(
            model="gpt-4", messages=[system_msg, user_msg], temperature=0.2
        )
        pr_summary = summary_resp.choices[0].message.content

        # 4) Post the summary as a comment
        comment_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"
        comment_payload = {"body": f"**PR Summary (AI):**\n\n{pr_summary}"}
        comment_res = requests.post(comment_url, headers=headers, json=comment_payload)
        print(f"Posted summary comment: {comment_res.status_code} {comment_res.text}")

        # 5) Simple risk check → auto-approve if tiny PR
        total_changes = sum(f.get("changes", 0) for f in files)
        reviews_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews"

        if total_changes < 10:
            approve_payload = {"event": "APPROVE", "body": "✅ Auto-approved by AI bot"}
            approve_res = requests.post(reviews_url, headers=headers, json=approve_payload)
            print(f"Posted auto-approve review: {approve_res.status_code} {approve_res.text}")
        else:
            # Otherwise, just leave a comment saying human review is recommended
            comment_review_payload = {"event": "COMMENT", "body": "AI review complete. Human review recommended."}
            comment_review_res = requests.post(
                reviews_url,
                headers=headers,
                json=comment_review_payload
            )
            print(f"Posted comment review: {comment_review_res.status_code} {comment_review_res.text}")

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(port=6000, debug=True)