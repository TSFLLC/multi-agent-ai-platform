import { registerRoute, startRouter } from "./router.js";
import { ensureToken } from "./tokenGate.js";
import { renderAsk } from "./pages/ask.js";
import { renderComparisons } from "./pages/comparisons.js";
import { renderComparisonDetail } from "./pages/comparisonDetail.js";
import { renderAgents } from "./pages/agents.js";
import { renderModels } from "./pages/models.js";
import { renderActivity } from "./pages/activity.js";
import { renderWorkflows } from "./pages/workflows.js";
import { renderWorkflowStudio } from "./pages/workflowStudio.js";
import { renderWorkflowRun } from "./pages/workflowRun.js";

registerRoute("/ask", renderAsk);
registerRoute("/comparisons", renderComparisons);
registerRoute("/comparisons/:id", renderComparisonDetail);
registerRoute("/agents", renderAgents);
registerRoute("/models", renderModels);
registerRoute("/activity", renderActivity);
registerRoute("/workflows", renderWorkflows);
registerRoute("/workflows/:id", renderWorkflowStudio);
registerRoute("/workflow-runs/:id", renderWorkflowRun);

// Locally this resolves immediately (the server already injected the
// token — see tokenGate.js). In hosted mode it blocks the app shell
// behind a login prompt until a valid token is entered (MA7.7B).
ensureToken().then(() => {
  startRouter(document.getElementById("app-root"), document.getElementById("app-nav"), "#/ask");
});
