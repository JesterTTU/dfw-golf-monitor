#!/usr/bin/env bash
# deploy_to_github.sh — Run this once from Terminal to create the GitHub repo
# and push all tee-time-monitor code.
#
# Prerequisites:
#   1. Sign in to GitHub in your browser so you can create a PAT
#   2. Have git installed (it is on your Mac by default)
#
# Usage:
#   chmod +x deploy_to_github.sh
#   ./deploy_to_github.sh YOUR_GITHUB_PAT

set -e

GITHUB_TOKEN="${1:?Usage: ./deploy_to_github.sh YOUR_GITHUB_PAT}"
GITHUB_USER="JesterTTU"
REPO_NAME="dfw-golf-monitor"
DISCORD_WEBHOOK="https://discord.com/api/webhooks/1519554853456838738/27ug6g7MyA4kG_7SIVPLgfG0hA8RQJ9jTQNPhWrPe374Ealf32J7MeTsZtobqe4IY0b9"

echo "==> Creating GitHub repo ${GITHUB_USER}/${REPO_NAME} ..."
curl -sf -X POST \
  -H "Authorization: token ${GITHUB_TOKEN}" \
  -H "Accept: application/vnd.github.v3+json" \
  https://api.github.com/user/repos \
  -d "{\"name\":\"${REPO_NAME}\",\"description\":\"DFW tee-time deal monitor for Tierra Verde & Texas Rangers\",\"private\":false,\"auto_init\":false}" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('Repo created:', d.get('html_url','(check GitHub)'))"

echo ""
echo "==> Initialising git and pushing code ..."
cd "$(dirname "$0")"   # tee-time-monitor directory

git init -q
git remote remove origin 2>/dev/null || true
git remote add origin "https://${GITHUB_TOKEN}@github.com/${GITHUB_USER}/${REPO_NAME}.git"
git add -A
git commit -q -m "feat: initial tee-time monitor — live API, Discord alerts, GitHub Actions cron"

# Create main branch and push
git branch -M main
git push -u origin main

echo ""
echo "==> Adding DISCORD_WEBHOOK secret to GitHub Actions ..."
# GitHub secrets require the value to be encrypted with the repo's public key
# Easiest: use the API to set it via the secrets endpoint
# We'll store it as a raw secret using the gh API workaround with libsodium
# Since we don't have gh cli, use a Python helper:
python3 - << PYEOF
import base64, json, urllib.request, urllib.error

token  = "${GITHUB_TOKEN}"
user   = "${GITHUB_USER}"
repo   = "${REPO_NAME}"
secret_name  = "DISCORD_WEBHOOK"
secret_value = "${DISCORD_WEBHOOK}"

headers = {
    "Authorization": f"token {token}",
    "Accept": "application/vnd.github.v3+json",
    "Content-Type": "application/json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# 1. Get repo public key for secret encryption
req = urllib.request.Request(
    f"https://api.github.com/repos/{user}/{repo}/actions/secrets/public-key",
    headers=headers
)
with urllib.request.urlopen(req) as r:
    pk = json.loads(r.read())

key_id  = pk["key_id"]
pub_key = pk["key"]

# 2. Encrypt the secret with libsodium (PyNaCl)
try:
    from nacl import encoding, public as nacl_public
    pk_bytes = base64.b64decode(pub_key)
    sealed = nacl_public.SealedBox(nacl_public.PublicKey(pk_bytes))
    encrypted = base64.b64encode(sealed.encrypt(secret_value.encode())).decode()
    
    # 3. Upload secret
    payload = json.dumps({"encrypted_value": encrypted, "key_id": key_id}).encode()
    req2 = urllib.request.Request(
        f"https://api.github.com/repos/{user}/{repo}/actions/secrets/{secret_name}",
        data=payload,
        headers=headers,
        method="PUT",
    )
    with urllib.request.urlopen(req2) as r:
        print(f"Secret {secret_name} added (status {r.status})")
except ImportError:
    print("PyNaCl not installed — adding secret manually:")
    print(f"  Go to: https://github.com/{user}/{repo}/settings/secrets/actions/new")
    print(f"  Name: {secret_name}")
    print(f"  Value: {secret_value}")
PYEOF

echo ""
echo "==> All done!"
echo "    Repo: https://github.com/${GITHUB_USER}/${REPO_NAME}"
echo "    Actions: https://github.com/${GITHUB_USER}/${REPO_NAME}/actions"
echo ""
echo "    To trigger a manual run right now:"
echo "    Visit the Actions tab and click 'Run workflow'"
