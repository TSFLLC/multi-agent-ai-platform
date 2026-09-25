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

