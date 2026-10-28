"""Unit tests for the scraper login logic, using HTML fixtures (no network)."""
from bs4 import BeautifulSoup

from ingest.scraper import MHCOScraper

LOGIN_FORM_HTML = """
<html><head><title>Log in | MHCO</title></head><body>
  <form id="user-login-form" action="/user/login?destination=/" method="post">
    <input type="text" name="name" />
    <input type="password" name="pass" />
    <input type="hidden" name="form_build_id" value="form-abc123XYZ" />
    <input type="hidden" name="form_id" value="user_login_form" />
    <input type="submit" name="op" value="Log in" />
  </form>
</body></html>
"""

LOGGED_IN_HTML = """
<html><head><title>My account | MHCO</title></head><body>
  <nav><a href="/user/42">My account</a><a href="/user/logout">Log out</a></nav>
  <h1>Welcome back</h1>
</body></html>
"""


def test_find_login_form_by_id():
    soup = BeautifulSoup(LOGIN_FORM_HTML, "html.parser")
    form = MHCOScraper._find_login_form(soup)
    assert form is not None and form.get("id") == "user-login-form"


def test_find_login_form_fallback_by_password_field():
    html = LOGIN_FORM_HTML.replace('id="user-login-form"', 'class="weird"')
    soup = BeautifulSoup(html, "html.parser")
    form = MHCOScraper._find_login_form(soup)
    assert form is not None


def test_build_login_payload_carries_token_and_sets_credentials():
    soup = BeautifulSoup(LOGIN_FORM_HTML, "html.parser")
    form = MHCOScraper._find_login_form(soup)
    payload = MHCOScraper._build_login_payload(form, "alice", "s3cret")
    assert payload["name"] == "alice"
    assert payload["pass"] == "s3cret"
    assert payload["form_build_id"] == "form-abc123XYZ"  # token preserved
    assert payload["form_id"] == "user_login_form"
    assert payload["op"] == "Log in"


def test_login_succeeded_detects_profile_redirect():
    page = BeautifulSoup("<html><body>ok</body></html>", "html.parser")
    assert MHCOScraper._login_succeeded("https://mhco.org/user/42", page) is True


def test_login_succeeded_detects_logout_link():
    page = BeautifulSoup(LOGGED_IN_HTML, "html.parser")
    assert MHCOScraper._login_succeeded("https://mhco.org/", page) is True


def test_login_failure_when_form_redisplayed():
    page = BeautifulSoup(LOGIN_FORM_HTML, "html.parser")
    # Still on the login form (password field present) => not logged in.
    assert MHCOScraper._login_succeeded("https://mhco.org/user/login", page) is False


def test_login_antibot_redirect_is_failure():
    # A bare antibot wall has no password field but is NOT a success.
    page = BeautifulSoup("<html><body>Please enable JavaScript</body></html>",
                         "html.parser")
    assert MHCOScraper._login_succeeded("https://mhco.org/antibot", page) is False


ANTIBOT_SETTINGS_HTML = """
<html><body>
  <form id="user-login-form" action="/antibot" method="post">
    <input type="text" name="name" />
    <input type="password" name="pass" />
    <input type="hidden" name="form_build_id" value="form-XYZ" />
    <input type="submit" name="op" value="Log in" />
  </form>
  <script type="application/json" data-drupal-selector="drupal-settings-json">
    {"antibot": {"key": "k3y-deadbeef",
                 "forms": {"user-login-form": {"action": "/user/login?destination=/"}}}}
  </script>
</body></html>
"""


def test_drupal_settings_extracts_antibot_key_and_action():
    soup = BeautifulSoup(ANTIBOT_SETTINGS_HTML, "html.parser")
    settings = MHCOScraper._drupal_settings(soup)
    antibot = settings["antibot"]
    assert antibot["key"] == "k3y-deadbeef"
    assert antibot["forms"]["user-login-form"]["action"] == "/user/login?destination=/"


# Real mhco.org shape: the key lives under forms[form_id].key, the real action
# is in the form's data-action attribute, and antibot_key is empty in markup.
MHCO_ANTIBOT_HTML = """
<html><body>
  <form action="/antibot" data-action="/user/login" class="user-login-form antibot"
        id="user-login-form" method="post">
    <input type="text" name="name" />
    <input type="password" name="pass" />
    <input type="hidden" name="form_build_id" value="form--abc" />
    <input type="hidden" name="form_id" value="user_login_form" />
    <input type="hidden" name="antibot_key" value="" />
    <button type="submit" name="op" value="Log in">Log in</button>
  </form>
  <script type="application/json" data-drupal-selector="drupal-settings-json">
    {"antibot": {"forms": {"user-login-form":
        {"id": "user-login-form", "key": "0mbdX_z0e5osEzRWRVaFwzHaq6EtxCnGc7DBKwPK1J0"}}}}
  </script>
</body></html>
"""


def test_mhco_antibot_key_lives_under_forms_entry():
    soup = BeautifulSoup(MHCO_ANTIBOT_HTML, "html.parser")
    settings = MHCOScraper._drupal_settings(soup)
    entry = settings["antibot"]["forms"]["user-login-form"]
    assert entry["key"] == "0mbdX_z0e5osEzRWRVaFwzHaq6EtxCnGc7DBKwPK1J0"
    # The real submit URL is the form's data-action, not its /antibot action.
    form = MHCOScraper._find_login_form(soup)
    assert form.get("action") == "/antibot"
    assert form.get("data-action") == "/user/login"
