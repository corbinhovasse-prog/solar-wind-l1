"""Static checks on the published dashboard and the automation: everything the page needs
exists, third-party code is pinned and verified, and nothing private or secret is published."""
import re
from html.parser import HTMLParser

import pytest

from conftest import ROOT

DOCS = ROOT / "docs"
HTML = (DOCS / "index.html").read_text(encoding="utf-8")
APP = (DOCS / "app.js").read_text(encoding="utf-8")
MAG = (DOCS / "magnetosphere.js").read_text(encoding="utf-8")
WORKFLOWS = {p.name: p.read_text(encoding="utf-8") for p in (ROOT / ".github/workflows").glob("*.yml")}


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.ids = [], set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append((tag, a))
        if "id" in a:
            self.ids.add(a["id"])


PAGE = Tags()
PAGE.feed(HTML)


# ---------------------------------------------------------------- the page is complete
def test_every_element_the_scripts_look_up_exists_in_the_page():
    wanted = set(re.findall(r'\$\("([\w-]+)"\)', APP + MAG)) | set(re.findall(r'getElementById\("([\w-]+)"\)', APP + MAG))
    assert wanted and not (wanted - PAGE.ids), f"missing ids: {sorted(wanted - PAGE.ids)}"


def test_element_ids_are_unique():
    ids = re.findall(r'\sid="([\w-]+)"', HTML)
    assert len(ids) == len(set(ids))


def test_local_files_the_page_loads_exist():
    for tag, a in PAGE.tags:
        ref = a.get("src") or (a.get("href") if tag == "link" else None)
        if ref and not ref.startswith(("http", "data:", "#")):
            assert (DOCS / ref).exists(), ref


def test_page_is_mobile_ready():
    metas = [a for t, a in PAGE.tags if t == "meta" and a.get("name") == "viewport"]
    assert metas and "width=device-width" in metas[0]["content"]
    assert "@media" in (DOCS / "style.css").read_text(encoding="utf-8")


# ---------------------------------------------------------------- third-party code
def test_every_external_script_is_pinned_and_integrity_checked():
    ext = [a for t, a in PAGE.tags if t == "script" and a.get("src", "").startswith("http")]
    assert ext
    for a in ext:
        assert a["src"].startswith("https://") and re.search(r"@\d+\.\d+\.\d+/", a["src"]), a["src"]
        assert a.get("integrity", "").startswith("sha384-") and a.get("crossorigin") == "anonymous"


def test_import_map_modules_are_pinned_and_integrity_checked():
    block = HTML.split('<script type="importmap">', 1)[1].split("</script>", 1)[0]
    urls = re.findall(r'"(https://[^"]+)"', block)
    assert urls and all(re.search(r"@\d+\.\d+\.\d+/", u) for u in urls if not u.endswith("/"))
    assert '"integrity"' in block


def test_no_insecure_or_dynamic_code_loading():
    for src in (HTML, APP, MAG):
        assert "http://" not in src.replace("http://www.w3.org", "")
        assert not re.search(r"\beval\(|new Function\(|document\.write\(", src)


# ---------------------------------------------------------------- privacy and secrets
# the project's own files only: never a local virtualenv, caches or generated data
SKIP_DIRS = {".git", ".venv", "venv", "env", "__pycache__", ".hypothesis", ".pytest_cache", "node_modules",
             "data", "sample_data", "site-packages"}
PUBLISHED = sorted(p for p in ROOT.rglob("*") if p.is_file()
                   and p.suffix in {".py", ".js", ".html", ".css", ".md", ".yml", ".txt", ".json"}
                   and not SKIP_DIRS & set(p.relative_to(ROOT).parts))
PHONE = re.compile(r"(?<![\w.])\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\w.])")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
SECRET = re.compile(r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY|xox[baprs]-|sk-[A-Za-z0-9]{20,}")


@pytest.mark.parametrize("path", PUBLISHED, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_phone_numbers_personal_emails_or_secrets_are_published(path):
    text = path.read_text(encoding="utf-8", errors="ignore")
    assert not PHONE.search(text), PHONE.search(text)
    # package@1.2.3 version pins look like addresses; only real domains count
    emails = [e for e in EMAIL.findall(text) if not e.split("@")[1][0].isdigit()
              and not e.endswith(("users.noreply.github.com", "noreply@anthropic.com"))]
    assert not emails, emails
    assert not SECRET.search(text)


def test_sample_and_local_data_are_not_committed():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8") if (ROOT / ".gitignore").exists() else ""
    assert "data/history/*" in ignore and "docs/data/latest.json" in ignore and ".env" in ignore


# ---------------------------------------------------------------- automation
def test_workflow_does_not_leave_the_token_on_disk():
    wf = WORKFLOWS["update.yml"]
    assert "persist-credentials: false" in wf


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_workflows_never_run_untrusted_pull_request_code_with_secrets(name):
    assert "pull_request_target" not in WORKFLOWS[name]


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_workflow_actions_are_pinned_to_a_version(name):
    for use in re.findall(r"uses:\s*(\S+)", WORKFLOWS[name]):
        assert "@" in use and not use.endswith(("@main", "@master")), use


def test_tests_run_in_ci_on_every_push():
    wf = WORKFLOWS.get("tests.yml", "")
    assert "pytest" in wf and "push" in wf and "permissions:" in wf
