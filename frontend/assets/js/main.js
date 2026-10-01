import { registerRoute, startRouter } from "./router.js";
import { ensureToken } from "./tokenGate.js";
import { renderAsk } from "./pages/ask.js";
import { renderComparisons } from "./pages/comparisons.js";
import { renderComparisonDetail } from "./pages/comparisonDetail.js";
import { renderAgents } from "./pages/agents.js";
import { renderModels } from "./pages/models.js";
import { renderModelExplorer } from "./pages/modelExplorer.js";
import { renderProviderExplorer } from "./pages/providerExplorer.js";
import { renderAgentRunDecision, renderModelIntelligence, renderRoutingDecision } from "./pages/modelIntelligence.js";
import { renderActivity } from "./pages/activity.js";
import { renderWorkflows } from "./pages/workflows.js";
import { renderWorkflowStudio } from "./pages/workflowStudio.js";
import { renderWorkflowRun } from "./pages/workflowRun.js";
import { renderToday, renderRadar, renderDevelopmentDetail } from "./pages/radar.js";
import { renderLab, renderExperimentDetail } from "./pages/lab.js";
import { renderProfessor } from "./pages/professor.js";
import { renderAssessmentCenter, renderAssessmentDefinition, renderAssessmentAttempt, renderDemonstrationRecord, renderReviewQueue, renderReviewDetail } from "./pages/assessments.js";
import { renderAcademyHome, renderAcademyProgram, renderAcademyLesson, renderAcademyToday, renderAcademyProgress, renderProjectLibrary, renderProjectOverview, renderBuildWorkspace } from "./pages/academy.js";
import { installReturnBanner } from "./workspace/returnBanner.js";
import { renderAcademy as renderLevel1Academy, renderAcademyDay as renderLevel1AcademyDay } from "./pages/academy-level1.js";

registerRoute("/ask", renderAsk);
registerRoute("/professor", renderProfessor);
registerRoute("/comparisons", renderComparisons);
registerRoute("/comparisons/:id", renderComparisonDetail);
registerRoute("/agents", renderAgents);
registerRoute("/models", renderModels);
registerRoute("/models/:id/explore", renderModelExplorer);
registerRoute("/providers/:id/explore", renderProviderExplorer);
registerRoute("/model-intelligence", renderModelIntelligence);
registerRoute("/model-intelligence/decisions/:id", renderRoutingDecision);
registerRoute("/model-intelligence/agent-runs/:id", renderAgentRunDecision);
registerRoute("/activity", renderActivity);
registerRoute("/workflows", renderWorkflows);
registerRoute("/workflows/:id", renderWorkflowStudio);
registerRoute("/workflow-runs/:id", renderWorkflowRun);
registerRoute("/ail/today", renderToday);
registerRoute("/ail/radar", renderRadar);
registerRoute("/ail/radar/developments/:id", renderDevelopmentDetail);
registerRoute("/ail/lab", renderLab);
registerRoute("/ail/lab/experiments/:id", renderExperimentDetail);
registerRoute("/academy", renderAcademyHome);
registerRoute("/academy/level-1", renderLevel1Academy);
registerRoute("/academy/level-1/:day", renderLevel1AcademyDay);
registerRoute("/academy/programs/:id", renderAcademyProgram);
registerRoute("/academy/concepts/:id", renderAcademyLesson);
registerRoute("/academy/enrollments/:id/today", renderAcademyToday);
registerRoute("/academy/enrollments/:id/progress", renderAcademyProgress);
registerRoute("/academy/projects", renderProjectLibrary);
registerRoute("/academy/projects/:id", renderProjectOverview);
registerRoute("/academy/projects/attempts/:id", renderBuildWorkspace);
registerRoute("/academy/assessments", renderAssessmentCenter);
registerRoute("/academy/assessments/definitions/:key", renderAssessmentDefinition);
registerRoute("/academy/assessments/attempts/:id", renderAssessmentAttempt);
registerRoute("/academy/assessments/records/:id", renderDemonstrationRecord);
registerRoute("/academy/assessments/reviews", renderReviewQueue);
registerRoute("/academy/assessments/reviews/:id", renderReviewDetail);

// Locally this resolves immediately (the server already injected the
// token — see tokenGate.js). In hosted mode it blocks the app shell
// behind a login prompt until a valid token is entered (MA7.7B).
ensureToken().then(() => {
  startRouter(document.getElementById("app-root"), document.getElementById("app-nav"), "#/ail/today");
  installReturnBanner();
});
