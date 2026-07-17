"""
Trinity Health Check — 项目健康度自检脚本
用法: python health_check.py

首次使用: 创建 .github_token 文件（已加入 .gitignore）
"""
import subprocess, json, os, sys
import urllib.request

# Read token from local config file (not committed)
TOKEN = ''
token_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.github_token')
if os.path.exists(token_file):
    TOKEN = open(token_file).read().strip()

OWNER = 'trinity-tick'
REPO = 'trinity'
API = f'https://api.github.com/repos/{OWNER}/{REPO}'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def gh_api(method, path='', data=None):
    if not TOKEN:
        return {'_error': 'No token. Create .github_token or set GITHUB_TOKEN env var.'}
    url = f'{API}/{path}' if path else API
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body,
        headers={'Authorization': f'token {TOKEN}',
                 'Accept': 'application/vnd.github+json'},
        method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except Exception as e:
        return {'_error': str(e)[:100]}

def check(ok, label, fix=""):
    icon = "OK" if ok else "FAIL"
    print(f"  [{icon}] {label}")
    if not ok and fix:
        print(f"         Fix: {fix}")

print(f"\n{'='*55}")
print(f"  Trinity Health Check")
print(f"  {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}")
print(f"{'='*55}")

# 1. Repository status
print("\n[Repository]")
if not TOKEN:
    check(False, "No token configured — skipping API checks")
else:
    repo = gh_api('GET', '')
    if 'name' in repo:
        check(True, f"Online: {repo['full_name']}")
        check(repo['visibility'] == 'public', f"Visibility: {repo['visibility']}")
        check(repo.get('has_discussions', False), "Discussions enabled")
        check(repo.get('has_issues', False), "Issues enabled")
        
        health = gh_api('GET', 'community/profile')
        if 'health_percentage' in health:
            hp = health['health_percentage']
            check(hp >= 90, f"Community health: {hp}%")
    else:
        check(False, f"Repository unreachable: {repo.get('_error','?')}")

# 2. PyPI
print("\n[PyPI]")
try:
    r = urllib.request.urlopen('https://pypi.org/pypi/trinity-memory/json', timeout=10)
    info = json.loads(r.read())['info']
    check(True, f"trinity-memory v{info['version']}")
except Exception as e:
    check(False, f"PyPI check failed: {str(e)[:50]}")

# 3. Workflows
print("\n[Workflows]")
if TOKEN:
    wf = gh_api('GET', 'actions/workflows')
    if 'workflows' in wf:
        for w in wf['workflows']:
            ok = w['state'] == 'active'
            check(ok, f"  {w['name']} ({w['state']})")
    else:
        check(False, f"Workflows unreachable: {wf.get('_error','?')}")

# 4. Local files
print("\n[Files]")
essential = [
    'README.md', 'README.zh.md', 'LICENSE', 'CODE_OF_CONDUCT.md',
    'CONTRIBUTING.md', 'ROADMAP.md', 'CHANGELOG.md', 'MANIFEST.in',
    'pyproject.toml', '.gitignore', 'mkdocs.yml',
    '.github/dependabot.yml', '.github/PULL_REQUEST_TEMPLATE.md',
    '.github/ISSUE_TEMPLATE/bug_report.md',
    '.github/ISSUE_TEMPLATE/feature_request.md',
    'trinity/py.typed', 'trinity/__init__.py',
]
for f in essential:
    ok = os.path.exists(os.path.join(BASE_DIR, f))
    check(ok, f"  {f}")

# 5. Git status
print("\n[Git]")
r = subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True, cwd=BASE_DIR)
uncommitted = [l for l in r.stdout.split('\n') if l.strip()]
check(len(uncommitted) == 0, f"Clean working tree ({len(uncommitted)} uncommitted)")

r = subprocess.run(['git', 'rev-list', '--count', 'HEAD..origin/main'],
                    capture_output=True, text=True, cwd=BASE_DIR)
behind = r.stdout.strip()
check(behind == '0' or behind == '', f"Behind remote: {behind or '0'} commits")

print(f"\n{'='*55}")
print(f"  Check complete!")
print(f"{'='*55}\n")
