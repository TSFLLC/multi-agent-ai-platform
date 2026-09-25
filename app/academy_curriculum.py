"""Initial AIL.5A curriculum manifest.

The manifest is content metadata only.  It resolves to the existing Concept
Graph at seed time and never creates a parallel lesson store.
"""

FOUNDATIONS_PROGRAM = {
    "slug": "practical-ai-foundations",
    "title": "30-Day Practical AI Foundations",
    "description": "A hands-on beginner path from AI fundamentals to a tested small AI project.",
    "duration_days": 30,
    "modules": [
        ("week-1-understand-ai", "Understand AI", [
            "what-ai-is-and-isnt", "generative-ai-llms", "models-providers-applications", "tokens",
            "context-windows", "inference", "hallucination-grounding", "evaluating-ai-claims",
            "privacy-responsible-ai-use",
        ]),
        ("week-2-use-ai-effectively", "Use AI Effectively", [
            "prompt-structure", "system-instructions", "few-shot-prompting", "json-basics",
            "structured-output", "iterating-debugging-ai-outputs", "model-pricing-token-economics",
            "choosing-a-model", "evaluation",
        ]),
        ("week-3-build-with-ai", "Build With AI", [
            "how-apps-call-models", "prompt-templates", "conversation-memory", "embeddings", "rag",
            "tool-calling", "agents", "workflows", "application-evaluation",
        ]),
        ("week-4-real-project", "Build a Real AI Project", [
            "problem-framing-requirements", "evaluation", "model-pricing-token-economics",
            "privacy-responsible-ai-use", "hallucination-grounding", "structured-output",
            "prompt-templates", "agents", "rag",
        ]),
    ],
}

# Build With Me content is versioned data, not service behavior.  Concept
# slugs are resolved by the seed service and missing graph content fails
# explicitly rather than creating duplicate Concepts.
BUILD_WITH_ME_PROJECTS = [
    {"key": "p1-trustworthy-explainer", "title": "Trustworthy Explainer", "brief": "Understand AI and build an explainer that distinguishes evidence from claims.", "mode": "no_code", "level": "l1", "concepts": ["evaluating-ai-claims", "hallucination-grounding", "privacy-responsible-ai-use"], "milestones": ["Frame the question", "Draft the explanation", "Test trustworthiness", "Explain your evidence"]},
    {"key": "p2-structured-extractor", "title": "Structured Extractor", "brief": "Use AI effectively to extract reliable structured information.", "mode": "workflow", "level": "l2", "concepts": ["prompt-structure", "structured-output", "json-basics", "evaluation"], "milestones": ["Define a schema", "Design the prompt", "Test representative inputs", "Improve failure handling"]},
    {"key": "p3-document-qa", "title": "Document Q&A Assistant", "brief": "Build a document question-answering experience with grounded responses.", "mode": "workflow", "level": "l3", "concepts": ["rag", "embeddings", "hallucination-grounding", "application-evaluation"], "capability": "ma9.mcp_runtime", "milestones": ["Define answer boundaries", "Plan retrieval", "Test grounding", "Explain limitations"]},
    {"key": "p4-project-direction", "title": "Capstone Preparation / Project Direction", "brief": "Prepare a real AI project direction, evidence plan, and next build decision.", "mode": "experiment", "level": "l3", "concepts": ["problem-framing-requirements", "evaluation", "model-pricing-token-economics", "privacy-responsible-ai-use"], "milestones": ["Frame the problem", "Choose an experiment", "Interpret results", "Prepare the handoff"]},
]

