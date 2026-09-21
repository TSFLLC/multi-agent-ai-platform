"""Local Operator Console static shell — MA5-UI.

Covers app.web: the console's HTML is served at "/", the real local MA1B
token (never a placeholder, never a hard-coded value) is injected into it,
and static assets are reachable — without needing a browser.
"""


def test_console_index_served_without_auth(client, bootstrap):
    """The shell page itself carries no API data — only same-origin JS
    that will call the real, auth-protected API — so it is reachable
    without a Bearer token, exactly like any other static asset would be."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Operator Console" in resp.text


def test_console_index_injects_real_local_token(client, bootstrap, auth_token):
    resp = client.get("/")
    assert resp.status_code == 200
    assert auth_token in resp.text
    assert "%%MAP_LOCAL_TOKEN_VALUE%%" not in resp.text  # placeholder must be replaced, not left in
    assert "window.__MAP_LOCAL_TOKEN__" in resp.text  # the JS global name itself must stay intact


def test_console_static_assets_served(client, bootstrap):
    resp = client.get("/assets/js/main.js")
    assert resp.status_code == 200
    assert "registerRoute" in resp.text

    resp_css = client.get("/assets/css/app.css")
    assert resp_css.status_code == 200


def test_console_routes_never_shadow_the_api(client, bootstrap, auth_headers):
    """"/assets" and "/" are mounted last (app.main) specifically so they
    can never intercept a real resource route."""
    resp = client.get("/health")
    assert resp.status_code == 200
    resp = client.get("/me", headers=auth_headers)
    assert resp.status_code == 200


# --- MA7.6A: Workflow Studio ---------------------------------------------------------------

EXISTING_NAV = ("ask", "comparisons", "agents", "models", "activity")
EXISTING_ROUTES = ("/ask", "/comparisons", "/comparisons/:id", "/agents", "/models", "/activity")
STUDIO_ASSETS = (
    "js/projectSelection.js",
    "js/workflowErrors.js",
    "js/workflowGraph.js",
    "js/dagLayout.js",
    "js/dagCanvas.js",
    "js/nodeInspector.js",
    "js/workflowApi.js",
    "js/pages/workflows.js",
    "js/pages/workflowStudio.js",
)


def test_console_navigation_has_workflows_and_keeps_every_existing_link(client, bootstrap):
    html = client.get("/").text
    assert 'href="#/workflows"' in html and 'data-route="workflows"' in html and ">Workflows<" in html
    for route in EXISTING_NAV:
        assert f'href="#/{route}"' in html and f'data-route="{route}"' in html, route


def test_console_registers_the_workflow_routes_and_keeps_the_existing_ones(client, bootstrap):
    main = client.get("/assets/js/main.js").text
    for route in ("/workflows", "/workflows/:id") + EXISTING_ROUTES:
        assert f'registerRoute("{route}"' in main, route
    assert 'import { renderWorkflows } from "./pages/workflows.js"' in main
    assert 'import { renderWorkflowStudio } from "./pages/workflowStudio.js"' in main


def test_every_studio_asset_is_served_as_javascript(client, bootstrap):
    for asset in STUDIO_ASSETS:
        response = client.get(f"/assets/{asset}")
        assert response.status_code == 200, asset
        assert "javascript" in response.headers["content-type"], asset
        assert "export " in response.text, asset


def test_studio_styles_are_served_and_existing_styles_are_intact(client, bootstrap):
    css = client.get("/assets/css/app.css").text
    for selector in (".studio-canvas", ".studio-node", ".studio-inspector", ".studio-validation"):
        assert selector in css, selector
    for selector in (".candidate-card", ".modal-panel", ".model-picker", ".rendered-answer"):
        assert selector in css, selector  # nothing pre-existing was removed


def test_the_studio_scripts_never_inject_html(client, bootstrap):
    """User-controlled values (node keys, Agent names, labels, instruction text) must only ever
    reach the DOM as text: none of the new scripts may use innerHTML or dom.el's ``html`` attribute."""
    for asset in STUDIO_ASSETS:
        code = client.get(f"/assets/{asset}").text
        stripped = "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("//"))
        assert "innerHTML" not in stripped, asset
        assert "outerHTML" not in stripped and "insertAdjacentHTML" not in stripped, asset
        assert "html:" not in stripped, asset
        assert "document.write" not in stripped and "eval(" not in stripped, asset


def test_the_old_pages_do_not_depend_on_the_studio_modules(client, bootstrap):
    for page in ("ask", "comparisons", "comparisonDetail", "agents", "models", "activity"):
        code = client.get(f"/assets/js/pages/{page}.js").text
        for asset in ("workflowGraph", "dagLayout", "dagCanvas", "nodeInspector", "workflowApi", "projectSelection"):
            assert asset not in code, (page, asset)
    state = client.get("/assets/js/state.js").text
    assert "projects[0]" in state  # the shared getProjectId is untouched; only the Workflow pages select explicitly
